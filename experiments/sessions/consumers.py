import argparse
import json
from pathlib import Path
import threading
import time

from common import Driver, Trace, snapshot, wait_until
from server import Service


def rss_kib(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except FileNotFoundError:
        pass
    return None


def trial(adapter, path, delay_ms, repetition, count, rate, trace, output):
    started = trace.add("trial_started", adapter=adapter, path=path,
                        delay_ms=delay_ms, repetition=repetition)
    service = Service(trace)
    tag = f"{adapter}-{path}-{delay_ms}-{repetition}"
    driver = Driver(adapter, service.address, output / f"{tag}.stderr", path, delay_ms, 50)
    samples = []
    stop = threading.Event()
    def sample():
        while not stop.is_set():
            if (value := rss_kib(driver.process.pid)) is not None:
                samples.append({"monotonic_ns": time.monotonic_ns(), "rss_kib": value})
            stop.wait(.02)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        baseline = driver.command("stats")
        service.send_packets(count, rate)
        marker = trace.wait(lambda row: row["event"] == "arbitration_emit" and row["code"] == 6
                            and row["sequence"] > started["sequence"])
        def backup():
            state = driver.command("state")
            return state if not state["is_primary"] else None
        observed = wait_until(backup)
        observation = trace.add("backup_observed", client_monotonic_ns=observed["monotonic_ns"])
        last_count, unchanged = -1, time.monotonic()
        def drained():
            nonlocal last_count, unchanged
            row = driver.command("stats")
            if len(row["processed"]) != last_count:
                last_count, unchanged = len(row["processed"]), time.monotonic()
            if not row["in_progress"] and row["queue_depth"] == 0 and time.monotonic()-unchanged >= .15:
                return row
        final = wait_until(drained)
        assert len(service.streams) == 1
        assert final["is_primary"] is False
        assert len(set(final["arrived"])) == len(final["arrived"])
        assert len(set(final["processed"])) == len(final["processed"])
        assert final["processed"] == sorted(final["processed"])
        assert set(final["processed"]).issubset(final["arrived"])
        assert all(0 <= number < count for number in final["arrived"])
        assert len(final["processed"]) + final["application_dropped"] == len(final["arrived"])
        if adapter == "sdk" and path == "native":
            assert final["processed"] == list(range(count))
        stop.set()
        sampler.join(timeout=2)
        assert not sampler.is_alive()
        frozen_samples = [dict(sample) for sample in samples]
        row = {"adapter": adapter, "path": path, "delay_ms": delay_ms, "repetition": repetition,
               "sent": count, "offered_rate_pps": rate, "application_capacity": 50 if path == "queued" else None,
               "library_packet_queue_capacity": 50 if adapter == "finsy" else None,
               "baseline": baseline, "final": final, "delivered": len(final["processed"]),
               "not_processed": count-len(final["processed"]),
               "not_delivered_to_iterator": count-len(final["arrived"]),
               "application_dropped": final["application_dropped"],
               "marker_sequence": marker["sequence"], "observation_sequence": observation["sequence"],
               "marker_to_observed_backup_ns": observed["monotonic_ns"]-marker["monotonic_ns"],
               "rss_samples": frozen_samples, "baseline_rss_kib": frozen_samples[0]["rss_kib"] if frozen_samples else None,
               "peak_sampled_rss_kib": max(sample["rss_kib"] for sample in frozen_samples) if frozen_samples else None,
               "start_sequence": started["sequence"]}
        return row
    finally:
        stop.set()
        sampler.join(timeout=2)
        driver.close()
        service.close()
        if "row" in locals():
            row["driver_exit_code"] = driver.process.returncode
            row["close_record"] = next(item for item in driver.rows if item.get("kind") == "reply"
                                       and item.get("op") == "close")
            assert driver.process.returncode == 0
            row["end_sequence"] = trace.add("trial_completed", adapter=adapter, path=path)["sequence"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--rate", type=int, default=500)
    parser.add_argument("--adapters", nargs="+", default=["sdk", "finsy"])
    parser.add_argument("--paths", nargs="+", default=["native", "queued"])
    parser.add_argument("--delays", type=int, nargs="+", default=[0, 1, 10])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    trace = Trace(args.output / "rpc.jsonl")
    rows = []
    try:
        with (args.output / "observations.jsonl").open("w") as file:
            for adapter in args.adapters:
                for path in args.paths:
                    for delay in args.delays:
                        for repetition in range(1, args.repetitions+1):
                            row = trial(adapter, path, delay, repetition, args.count, args.rate, trace, args.output)
                            rows.append(row)
                            file.write(json.dumps(row)+"\n")
                            file.flush()
                            print(f"{adapter} {path} {delay}ms: {repetition}/{args.repetitions}", flush=True)
    finally:
        trace.close()
    snapshot(args.output, {"kind": "consumers", "trials": len(rows), "repetitions": args.repetitions,
                           "packet_count": args.count, "offered_rate_pps": args.rate,
                           "adapters": args.adapters, "paths": args.paths, "delays_ms": args.delays,
                           "drain_quiet_seconds": .15, "rss_sampling_seconds": .02,
                           "state_poll_seconds": .005})


if __name__ == "__main__":
    main()
