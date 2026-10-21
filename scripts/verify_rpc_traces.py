"""Verify original RPC requests, readbacks and published per-round measurements."""

import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import random
import sys

import numpy as np
from finsy.proto import p4i, p4r
from google.protobuf import json_format, text_format

from rpc_trace_files import (
    ROOT, TRACE_DATA, archive_members, check_file, file_hash, load_catalog,
    load_inventory, require, selected_points,
)

sys.path.insert(0, str(ROOT / "experiments/performance"))
from audit import check_run

STEPS = [
    ("insert", "measurement"), ("modify", "warmup"), ("modify", "measurement"),
    ("read_one", "warmup"), ("read_one", "measurement"),
    ("read_all", "warmup"), ("read_all", "measurement"),
]


class TableDecoder:
    def __init__(self, info):
        require(len(info.tables) == 1, "Expected one table")
        self.table = info.tables[0]
        self.action = next(action for action in info.actions if action.preamble.name == "MyIngress.forward")
        require(self.table.size == 32768 and len(self.table.match_fields) == 1 and
                self.table.match_fields[0].bitwidth == 32, "Unexpected table schema")
        require(len(self.action.params) == 1 and self.action.params[0].bitwidth == 9,
                "Unexpected action schema")

    def entry(self, item):
        require(item.table_id == self.table.preamble.id and len(item.match) == 1,
                "Unexpected table or match count")
        field = item.match[0]
        require(field.field_id == self.table.match_fields[0].id and
                field.WhichOneof("field_match_type") == "exact" and 0 < len(field.exact.value) <= 4,
                "Unexpected match encoding")
        require(item.action.WhichOneof("type") == "action", "Expected a direct action")
        action = item.action.action
        require(action.action_id == self.action.preamble.id and len(action.params) == 1,
                "Unexpected action")
        param = action.params[0]
        require(param.param_id == self.action.params[0].id and 0 < len(param.value) <= 2,
                "Unexpected action parameter")
        require(item.metadata == b"E2" and not item.is_default_action and item.priority == 0,
                "Unexpected entry metadata")
        key, port = int.from_bytes(field.exact.value, "big"), int.from_bytes(param.value, "big")
        require(port <= 511, "Port exceeds its declared width")
        return key, port

    def ready(self, row):
        require(row["ready"] is True and row["rpc_deadline_seconds"] == 10 and
                len(row["wire_samples"]) == 3, "Invalid driver readiness record")
        for sample, expected in zip(row["wire_samples"], [(1, 1), (256, 2), (0xffffffff, 511)]):
            require(self.entry(json_format.ParseDict(sample, p4r.TableEntry())) == expected,
                    "Driver wire sample differs")

    def state(self, packed, record, expected):
        require(hashlib.sha256(packed).hexdigest() == record["gzip_sha256"], "Readback gzip hash differs")
        raw = gzip.decompress(packed)
        require(hashlib.sha256(raw).hexdigest() == record["protobuf_sha256"], "Readback protobuf hash differs")
        response = p4r.ReadResponse.FromString(raw)
        actual = {}
        for entity in response.entities:
            require(entity.WhichOneof("entity") == "table_entry", "Unexpected readback entity")
            key, port = self.entry(entity.table_entry)
            require(key not in actual, "Duplicate readback key")
            actual[key] = port
        require(record["state_matches"] is True and len(actual) == record["entries"] and actual == expected,
                "Full table state differs from the request outcomes")


def phase_metrics(row):
    records = row["records"]
    elapsed = row["elapsed_ns"] / 1e9
    successful = sum(record["rpc_code"] == 0 for record in records)
    result = dict(
        attempts=len(records), successful_rpcs_per_second=successful / elapsed,
        confirmed_updates_per_second=row["ok_updates"] / elapsed,
        failed_calls=row["failed_calls"], unknown_updates=row["unknown_updates"],
        read_entities_per_second=sum(record["read_count"] for record in records
                                     if record["rpc_code"] == 0) / elapsed,
    )
    for prefix, start, end in (("", "start_ns", "end_ns"),
                               ("build_", "start_ns", "build_end_ns"),
                               ("call_", "build_end_ns", "end_ns")):
        values = np.fromiter((record[end] - record[start] for record in records), dtype=np.int64)
        if prefix:
            result[prefix + "mean_ns"] = float(values.mean())
        for label, q in (("p50", .5), ("p95", .95), ("p99", .99)):
            result[prefix + label + "_ns"] = int(np.quantile(values, q, method="inverted_cdf"))
    resources = row["resources"]
    ticks_per_second = resources["clock_ticks_per_second"]
    require(ticks_per_second > 0, "Invalid process tick rate")
    for name in ("client", "target"):
        before, after = resources["before"][name], resources["after"][name]
        seconds = (after["monotonic_ns"] - before["monotonic_ns"]) / 1e9
        ticks = [after[key] - before[key] for key in ("user_ticks", "system_ticks")]
        require(seconds > 0 and all(value >= 0 for value in ticks), "Invalid resource interval")
        rss = [before["rss_kib"], after["rss_kib"]]
        rss.extend(sample[name]["rss_kib"] for sample in resources["rss_samples"])
        require(all(value is not None and value > 0 for value in rss), "Invalid RSS sample")
        result[name + "_cpu_core_percent"] = 100 * sum(ticks) / ticks_per_second / seconds
        result[name + "_peak_rss_kib"] = max(rss)
        result[name + "_observation_seconds"] = seconds
    return result


def compare_metrics(row, saved):
    for field in ("profile", "operation", "phase"):
        require(row[field] == saved[field], f"Measurement field differs: {field}")
    for field in ("repetition", "n", "batch", "concurrency", "elapsed_ns"):
        require(row[field] == int(saved[field]), f"Measurement field differs: {field}")
    checked = 0
    for field, value in phase_metrics(row).items():
        require(np.isfinite(float(saved[field])) and
                np.isclose(value, float(saved[field]), rtol=1e-11, atol=1e-7),
                f"Measurement metric differs: {field}")
        checked += 1
    return checked


def check_run_metadata(run, point, dataset):
    require(run["profiles"] == dataset["profiles"] and run["repetitions"] == dataset["repetitions"] == 10,
            "Run profiles or repetitions differ")
    require(run["configurations"] == [[point[key] for key in ("n", "batch", "concurrency")]],
            "Run parameters differ")
    require(run["phases"] == 420 and not run["inject_failure"] and
            run["warmup_seconds"] == 5 and run["measurement_seconds"] == 30 and
            run["rpc_deadline_seconds"] == 10 and run["protocol_revision"] == 2,
            "Run method differs")
    rng = random.Random(run["random_seed"])
    order = []
    for _ in range(10):
        profiles = list(run["profiles"])
        rng.shuffle(profiles)
        order.append(profiles)
    require(order == run["profile_order"] == point["profile_order"], "Run order differs")


def verify_point(directory, point, dataset, decoder):
    inventory = load_inventory(point)
    definition = next(item for item in dataset["points"] if item["id"] == point["id"])
    name = f"data/rpc/{point['id']}/measurement-rounds.csv"
    check_file(ROOT / name, dataset["files"][name])
    with (ROOT / name).open(newline="") as stream:
        measurements = {(row["profile"], int(row["repetition"]), row["operation"]): row
                        for row in csv.DictReader(stream)}
    require(len(measurements) == 240, "Incomplete measurement CSV")
    run = None
    state_record = None
    expected = {}
    phases = requests = checked = measured = states = peak = 0
    for record, data in archive_members(directory / point["archive"]["name"], point, inventory):
        name = Path(record["name"]).name
        if name == "run.json":
            run = json.loads(data)
            check_run_metadata(run, definition, dataset)
        elif name == "drivers.json":
            drivers = json.loads(data)
            actual = [(r["repetition"], r["profile"]) for r in drivers]
            require(run is not None and actual == [(i + 1, p) for i, order in enumerate(run["profile_order"])
                                                   for p in order], "Driver order differs")
            require(all(row["exit_code"] == 0 for row in drivers), "Driver exited unsuccessfully")
        elif name == "target-processes.json":
            targets = json.loads(data)
            require(len(targets) == 1 and targets[0]["stop_ns"] >= targets[0]["start_ns"] and
                    isinstance(targets[0]["exit_code"], int), "Target termination record differs")
        elif name.startswith("phase-"):
            require(run is not None and state_record is None and name == f"phase-{phases}.json",
                    "Unexpected phase sequence")
            row = json.loads(data)
            repetition, position = divmod(phases, 42)
            profile_position, step = divmod(position, 7)
            profile = run["profile_order"][repetition][profile_position]
            operation, phase = STEPS[step]
            require((row["profile"], row["repetition"], row["operation"], row["phase"], row["index"]) ==
                    (profile, repetition + 1, operation, phase, phases), "Phase identity differs")
            require(row["ready"]["profile"] == profile, "Driver profile differs")
            require(all(row[key] == definition[key] for key in ("n", "batch", "concurrency")),
                    "Phase parameters differ")
            for field, value in record["phase"].items():
                require(row[field] == value, f"Phase inventory differs: {field}")
            seconds = 0 if operation == "insert" else 5 if phase == "warmup" else 30
            require(row["requested_seconds"] == seconds and row["command"] ==
                    dict(op="run", operation=operation, n=row["n"], batch=row["batch"],
                         concurrency=row["concurrency"], seconds=seconds, phase=phase), "Phase command differs")
            require(row["failed_calls"] == row["unknown_updates"] == 0 and not row["oracle_events"],
                    "Unexpected request failure or proxy event")
            decoder.ready(row["ready"])
            if step == 0:
                expected = {}
            expected, observed_peak = check_run(row, expected)
            require(row["peak_outstanding_rpcs"] == observed_peak, "Concurrency peak differs")
            peak = max(peak, observed_peak)
            requests += row["attempts"]
            if phase == "measurement":
                checked += compare_metrics(row, measurements.pop((profile, repetition + 1, operation)))
                measured += 1
            state_record = row["state"]
            require(state_record["file"] == f"state-{phases}.pb.gz" and state_record["entries"] == row["n"],
                    "State reference differs")
            phases += 1
            del row
        elif name.startswith("state-"):
            require(state_record is not None and name == state_record["file"], "Unexpected readback")
            decoder.state(data, state_record, expected)
            state_record = None
            states += 1
            if states % 42 == 0:
                print(f"{point['id']}: verified {states}/420 phases", flush=True)
        else:
            raise ValueError(f"Unknown archive member: {name}")
    require(phases == states == point["phases"] == 420 and measured == point["measurement_phases"] == 240 and
            not measurements and requests == point["requests"] and state_record is None,
            "Incomplete request or measurement coverage")
    return dict(point=point["id"], phases=phases, measurement_rows=measured, full_state_checks=states,
                requests=requests, numeric_fields_checked=checked, max_outstanding_rpcs=peak,
                failed_calls=0, driver_exits=60, owned_target_exits=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", nargs="+", default=["default"], help="Parameter names, or all")
    parser.add_argument("--directory", type=Path, default=ROOT / "results/rpc-traces")
    parser.add_argument("--output", type=Path, default=ROOT / "results/rpc-trace-verification.json")
    args = parser.parse_args()
    require(__debug__, "Run without -O so request-audit assertions remain enabled")
    require(not args.output.resolve().is_relative_to(ROOT / "data"), "Write reports outside data/")
    catalog, dataset = load_catalog()
    info = text_format.Parse((TRACE_DATA / "control.p4info.txt").read_text(), p4i.P4Info())
    decoder = TableDecoder(info)
    results = []
    for point in selected_points(catalog, args.points):
        results.append(verify_point(args.directory, point, dataset, decoder))
    report = dict(passed=True, points=results,
                  catalog_sha256=file_hash(TRACE_DATA / "manifest.json"),
                  verifier_sha256=file_hash(Path(__file__)),
                  audit_sha256=file_hash(ROOT / "experiments/performance/audit.py"),
                  archive_reader_sha256=file_hash(ROOT / "scripts/rpc_trace_files.py"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
