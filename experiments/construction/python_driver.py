"""Offline construction driver using the pinned Python installations."""

import argparse
import contextlib
import gc
import json
import platform
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parent


class Finsy:
    def __init__(self):
        import finsy as fy
        self.fy = fy
        self.schema = fy.P4Schema(ROOT / "fixtures/construct.p4info.txt")

    def factory(self, corpus, mode):
        fy = self.fy

        def make(sample):
            return fy.P4TableEntry(sample["table"], match=fy.P4TableMatch(sample["match_values"]),
                                   action=fy.P4TableAction(sample["action"], **sample["param_values"]),
                                   priority=sample["priority"], metadata=sample["metadata_bytes"])

        cached = make(corpus[0]) if mode == "reused" else None

        def build(sample):
            item = cached
            if item is None:
                item = make(sample)
            else:
                item.match.update(sample["match_values"])
                item.action.args.update(sample["param_values"])
                item.priority = sample["priority"]
                item.metadata = sample["metadata_bytes"]
            return item.encode_entry(self.schema)

        return build


class Shell:
    def __init__(self):
        import p4runtime_sh.shell as sh
        from p4.config.v1 import p4info_pb2
        from google.protobuf import text_format
        self.sh = sh
        sh.context.set_p4info(text_format.Parse((ROOT / "fixtures/construct.p4info.txt").read_text(), p4info_pb2.P4Info()))
        sh.global_options["verbose"] = False

    def factory(self, corpus, mode):
        sh = self.sh

        def make(sample):
            item = sh.TableEntry(sample["table"])
            item.action = sh.Action(sample["action"])
            return item

        cached = make(corpus[0]) if mode == "reused" else None

        def build(sample):
            item = cached if cached is not None else make(sample)
            for key, value in sample["match_strings"].items():
                item.match[key] = value
            for key, value in sample["param_strings"].items():
                item.action[key] = value
            item.priority = sample["priority"]
            item.metadata = sample["metadata_bytes"]
            return item.msg()

        return build


def prepare_inputs():
    inputs = json.loads((ROOT / "fixtures/inputs.json").read_text())
    for corpus in inputs.values():
        for sample in corpus:
            values, strings = {}, {}
            for match in sample["matches"]:
                value = int(match["value"], 16)
                text = "0x" + match["value"]
                if match["kind"] == "lpm":
                    value = (value, match["prefix"])
                    text += "/" + str(match["prefix"])
                elif match["kind"] == "ternary":
                    value = (value, int(match["mask"], 16))
                    text += "&&&0x" + match["mask"]
                elif match["kind"] == "range":
                    value = (value, int(match["high"], 16))
                    text += "..0x" + match["high"]
                values[match["field"]] = value
                strings[match["field"]] = text
            sample.update(match_values=values, match_strings=strings,
                          param_values={p["name"]: int(p["value"], 16) for p in sample["params"]},
                          param_strings={p["name"]: "0x" + p["value"] for p in sample["params"]},
                          metadata_bytes=bytes.fromhex(sample["metadata"]))
    return inputs


def check(backend, profile, inputs):
    from google.protobuf import json_format
    samples = []
    for workload, corpus in inputs.items():
        for mode in ("new", "reused"):
            build = backend.factory(corpus, mode)
            for index, sample in enumerate(corpus):
                samples.append(dict(profile=profile, workload=workload, mode=mode, index=index,
                                    wire=json_format.MessageToDict(build(sample), preserving_proto_field_name=True)))
            first = build(corpus[0])
            before = first.SerializeToString(deterministic=True)
            mutable = corpus[0]
            original_matches, original_params, original_metadata = mutable["match_values"].copy(), mutable["param_values"].copy(), mutable["metadata_bytes"]
            original_match_strings, original_param_strings = mutable["match_strings"].copy(), mutable["param_strings"].copy()
            mutable["match_values"]["h.key.value"] = 1
            mutable["param_values"]["port"] = 2
            mutable["match_strings"]["h.key.value"] = "0x01"
            mutable["param_strings"]["port"] = "0x02"
            mutable["metadata_bytes"] = b"changed"
            assert first.SerializeToString(deterministic=True) == before
            mutable["match_values"].clear()
            mutable["match_values"].update(original_matches)
            mutable["param_values"].clear()
            mutable["param_values"].update(original_params)
            mutable["match_strings"].clear()
            mutable["match_strings"].update(original_match_strings)
            mutable["param_strings"].clear()
            mutable["param_strings"].update(original_param_strings)
            mutable["metadata_bytes"] = original_metadata
            second = build(corpus[1])
            assert first.SerializeToString(deterministic=True) == before
            second_before = second.SerializeToString(deterministic=True)
            first.match[0].exact.value = b"changed"
            first.action.action.params[0].value = b"changed"
            first.metadata = b"changed"
            assert second.SerializeToString(deterministic=True) == second_before
            assert mutable["match_values"] == original_matches and mutable["param_values"] == original_params
            assert mutable["match_strings"] == original_match_strings and mutable["param_strings"] == original_param_strings
    return samples


def measure(build, corpus, seconds):
    def loop(count):
        result = None
        started = time.monotonic_ns()
        for index in range(count):
            result = build(corpus[index & 255])
        elapsed = time.monotonic_ns() - started
        assert result is not None
        return elapsed

    count = 256
    while True:
        elapsed = loop(count)
        if elapsed >= 200_000_000:
            break
        count *= 2
    iterations = max(1, round(count * seconds * 1e9 / elapsed))
    gc.collect()
    elapsed = loop(iterations)
    return dict(iterations=iterations, elapsed_ns=elapsed, ns_per_op=elapsed / iterations,
                gc_enabled=gc.isenabled(), timing_basis="WSL CLOCK_MONOTONIC")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("finsy", "shell"), required=True)
    parser.add_argument("--workload", choices=("exact", "mixed"))
    parser.add_argument("--mode", choices=("new", "reused"))
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        inputs = prepare_inputs()
        backend = Finsy() if args.profile == "finsy" else Shell()
        if args.check:
            samples = check(backend, args.profile, inputs)
            args.check.write_text(json.dumps(samples) + "\n")
            result = dict(checked=len(samples), profile=args.profile, python=platform.python_version())
        else:
            result = measure(backend.factory(inputs[args.workload], args.mode), inputs[args.workload], args.seconds)
            result.update(profile=args.profile, workload=args.workload, mode=args.mode,
                          python=platform.python_version(), executable=sys.executable)
    print(json.dumps(result), file=output)


if __name__ == "__main__":
    main()
