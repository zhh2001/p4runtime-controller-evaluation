"""Package the selected formal RPC runs without changing their trace bytes."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

from rpc_trace_files import ROOT, file_hash, require

RELEASE = "rpc-traces-v1"
RUN_FIELDS = (
    "date", "created_utc", "kind", "repetitions", "profiles", "profile_order",
    "configurations", "warmup_seconds", "measurement_seconds", "phases",
    "inject_failure", "random_seed", "rpc_deadline_seconds", "protocol_revision",
)


def encode(value):
    return (json.dumps(value, indent=2) + "\n").encode()


def add_member(archive, name, data):
    if name.endswith(".json"):
        require(b"/home/" not in data and b"/mnt/" not in data, f"Local text found in {name}")
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))
    return dict(name=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


def package_point(source, output, index_output, point, dataset):
    original = json.loads((source / "manifest.json").read_text())
    require(original["profiles"] == dataset["profiles"] and
            original["repetitions"] == dataset["repetitions"] and
            original["profile_order"] == point["profile_order"], "Run order differs")
    require(original["configurations"] == [[point[key] for key in ("n", "batch", "concurrency")]],
            "Run parameters differ")
    require(original["warmup_seconds"] == 5 and original["measurement_seconds"] == 30 and
            not original["inject_failure"] and original["phases"] == 420,
            "Unexpected workload")
    briefs = [json.loads(line) for line in (source / "observations.jsonl").read_text().splitlines()]
    require(len(briefs) == 420, "Incomplete phase inventory")
    archive_path = output / f"{RELEASE}-{point['id']}.tar.gz"
    require(not archive_path.exists(), f"Output exists: {archive_path}")
    files = []
    with archive_path.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
                run = {key: original[key] for key in RUN_FIELDS}
                files.append(add_member(archive, point["id"] + "/run.json", encode(run)))
                for name in ("drivers.json", "target-processes.json"):
                    files.append(add_member(archive, point["id"] + "/" + name, (source / name).read_bytes()))
                for number, brief in enumerate(briefs):
                    require(brief["index"] == number and brief["detail_file"] == f"phase-{number}.json",
                            "Phase sequence differs")
                    for name in (f"phase-{number}.json", f"state-{number}.pb.gz"):
                        data = (source / name).read_bytes()
                        if name.endswith(".json"):
                            record = add_member(archive, point["id"] + "/" + name, data)
                            record["phase"] = {key: brief[key] for key in
                                               ("index", "profile", "repetition", "operation", "phase", "attempts")}
                        else:
                            require(hashlib.sha256(data).hexdigest() == brief["state"]["gzip_sha256"],
                                    f"Readback hash differs: {name}")
                            record = add_member(archive, point["id"] + "/" + name, data)
                        files.append(record)
                    if (number + 1) % 42 == 0:
                        print(f"{point['id']}: packed {number + 1}/420 phases", flush=True)
    require(archive_path.stat().st_size < 2 * 1024**3, "Archive exceeds the release asset limit")
    inventory = {
        "point": point["id"], "source_run": point["source_run"],
        "source_manifest_sha256": file_hash(source / "manifest.json"),
        "run_metadata_fields": list(RUN_FIELDS), "files": files,
    }
    index_path = index_output / (point["id"] + ".json")
    index_path.write_bytes(encode(inventory))
    return {
        "id": point["id"], "archive": {"name": archive_path.name,
        "bytes": archive_path.stat().st_size, "sha256": file_hash(archive_path)},
        "inventory": {"bytes": index_path.stat().st_size, "sha256": file_hash(index_path)},
        "phases": len(briefs), "measurement_phases": sum(r["phase"] == "measurement" for r in briefs),
        "requests": sum(row["attempts"] for row in briefs),
        "uncompressed_member_bytes": sum(item["bytes"] for item in files),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True,
                        help="Directory containing the source_run directories in the measurement manifest")
    parser.add_argument("--output", type=Path, required=True, help="New directory for release assets")
    parser.add_argument("--index-output", type=Path, default=ROOT / "data/rpc/traces")
    args = parser.parse_args()
    require(not args.output.resolve().is_relative_to(ROOT / "data"),
            "Write release archives outside the Git data directory")
    args.output.mkdir(parents=True, exist_ok=False)
    args.index_output.mkdir(parents=True, exist_ok=False)
    dataset = json.loads((ROOT / "data/rpc/manifest.json").read_text())
    infos = [(args.source_root / point["source_run"] /
              "source/experiments/performance/fixtures/control.p4info.txt").read_bytes()
             for point in dataset["points"]]
    require(all(info == infos[0] for info in infos), "P4Info differs across runs")
    require(b"/home/" not in infos[0] and b"/mnt/" not in infos[0], "Local text found in P4Info")
    (args.index_output / "control.p4info.txt").write_bytes(infos[0])
    catalog = {
        "schema_version": 1, "release_tag": RELEASE,
        "release_url": f"https://github.com/zhh2001/p4runtime-controller-evaluation/releases/tag/{RELEASE}",
        "download_base": f"https://github.com/zhh2001/p4runtime-controller-evaluation/releases/download/{RELEASE}",
        "dataset_manifest_sha256": file_hash(ROOT / "data/rpc/manifest.json"),
        "p4info": {"bytes": len(infos[0]), "sha256": hashlib.sha256(infos[0]).hexdigest()},
        "points": [],
    }
    for point in dataset["points"]:
        record = package_point(args.source_root / point["source_run"], args.output,
                               args.index_output, point, dataset)
        catalog["points"].append(record)
        print(json.dumps(record), flush=True)
    (args.index_output / "manifest.json").write_bytes(encode(catalog))
    print(json.dumps({"archives": len(catalog["points"]),
                      "compressed_bytes": sum(p["archive"]["bytes"] for p in catalog["points"])}))


if __name__ == "__main__":
    main()
