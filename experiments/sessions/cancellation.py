import argparse
import json
from pathlib import Path
import time

from common import Driver, Trace, snapshot, wait_until
from server import Service

PAYLOAD_BYTES = 2 * 1024 * 1024
BACKPRESSURE_PROBE_SECONDS = 1.0


def pending(driver, job, seconds=.2):
    try:
        driver.result(job, seconds)
        return False
    except TimeoutError:
        return True


def saturate(driver):
    accepted = []
    for job in range(1, 65):
        driver.command("start-send", job=job, size=PAYLOAD_BYTES)
        driver.wait(lambda row: row.get("kind") == "send_started" and row["job"] == job)
        try:
            result = driver.result(job, BACKPRESSURE_PROBE_SECONDS)
        except TimeoutError:
            return job, accepted
        assert result.get("rpc_code") == 0, result
        accepted.append(result)
    raise RuntimeError("flow-control backpressure was not observed")


def send_barrier(driver, service, trace, start):
    driver.command("start-send", job=99999, size=64)
    result = driver.result(99999)
    assert result.get("rpc_code") == 0, result
    received = trace.wait(lambda row: row["event"] == "packet_out_received"
                          and row["packet_id"] == 99999 and row["sequence"] >= start)
    return result, received


def trial(adapter, mode, repetition, trace, output):
    started = trace.add("trial_started", adapter=adapter, mode=mode, repetition=repetition)
    service = Service(trace, paused=mode not in ("pre_cancelled", "graceful_timeout"),
                      hold_final=mode in ("graceful", "graceful_timeout"))
    driver = Driver(adapter, service.address, output / f"{adapter}-{mode}-{repetition}.stderr")
    row = {"adapter": adapter, "mode": mode, "repetition": repetition,
           "start_sequence": started["sequence"]}
    try:
        if mode in ("inflight_cancelled", "queued_cancelled"):
            # Avoid testing Finsy's short-connection failure throttle here.
            time.sleep(2.1)
            blocked, accepted = saturate(driver)
            row.update(blocked_job=blocked, preceding_sends=accepted)
            assert pending(driver, blocked, .05)
            row["backpressure_sequence"] = trace.add("backpressure_observed", job=blocked)["sequence"]
            if mode == "queued_cancelled":
                assert adapter == "sdk"
                driver.command("start-send", job=20000, size=64)
                driver.wait(lambda item: item.get("kind") == "send_started" and item["job"] == 20000)
                assert pending(driver, 20000, .05)
                assert pending(driver, blocked, .01)
                driver.command("cancel", job=20000)
                row["cancelled_send"] = driver.result(20000)
                assert row["cancelled_send"]["cancelled"]
                service.read_gate.set()
                row["blocked_send"] = driver.result(blocked)
                assert row["blocked_send"].get("rpc_code") == 0
                row["barrier"], _ = send_barrier(driver, service, trace, started["sequence"])
                assert len(service.streams) == 1
            else:
                driver.command("cancel", job=blocked)
                row["cancelled_send"] = driver.result(blocked)
                assert row["cancelled_send"]["cancelled"]
                trace.wait(lambda item: item["event"] == "stream_closed"
                           and item["stream_id"] == 1 and item["sequence"] > started["sequence"])
                try:
                    wait_until(lambda: len(service.streams) >= 2 and
                               driver.command("state")["is_primary"], timeout=60)
                    row["recovered"] = True
                except TimeoutError:
                    row["recovered"] = False
                service.read_gate.set()
                if row["recovered"]:
                    row["barrier"], _ = send_barrier(driver, service, trace, started["sequence"])
        elif mode == "pre_cancelled":
            driver.command("start-send", job=20000, size=64, pre_cancelled=True)
            row["cancelled_send"] = driver.result(20000)
            assert row["cancelled_send"]["cancelled"]
            row["barrier"], _ = send_barrier(driver, service, trace, started["sequence"])
            assert len(service.streams) == 1
        else:
            driver.command("start-send", job=1, size=64)
            row["send"] = driver.result(1)
            assert row["send"].get("rpc_code") == 0
            timeout_ms = 200 if mode == "graceful_timeout" else 3000
            driver.command("start-close", job=90000, graceful=mode != "immediate", timeout_ms=timeout_ms)
            if mode == "graceful":
                time.sleep(.05)
                assert not any(item.get("kind") == "close_result" for item in driver.rows)
                service.read_gate.set()
                eof = trace.wait(lambda item: item["event"] == "request_eof"
                                 and item["sequence"] >= started["sequence"])
                time.sleep(.05)
                assert not any(item.get("kind") == "close_result" for item in driver.rows)
                row["request_eof_sequence"] = eof["sequence"]
                row["final_status_released_sequence"] = trace.add("final_status_released")["sequence"]
                service.final_gate.set()
            elif mode == "graceful_timeout":
                eof = trace.wait(lambda item: item["event"] == "request_eof"
                                 and item["sequence"] >= started["sequence"])
                row["request_eof_sequence"] = eof["sequence"]
            row["close"] = driver.wait(lambda item: item.get("kind") == "close_result")
            if adapter == "sdk":
                assert row["close"].get("is_primary") is False
            else:
                assert row["close"].get("is_up") is False
            if mode == "graceful_timeout":
                assert row["close"].get("deadline"), row
            else:
                assert row["close"].get("rpc_code") == 0, row
            trace.wait(lambda item: item["event"] == "stream_closed"
                       and item["sequence"] >= started["sequence"])
        row["final_state"] = driver.command("state")
        row["stream_count"] = len(service.streams)
    finally:
        driver.close()
        service.close()
        row["driver_exit_code"] = driver.process.returncode
        row["driver_records"] = driver.rows
        row["end_sequence"] = trace.add("trial_completed", adapter=adapter, mode=mode)["sequence"]
        row["server_events"] = trace.rows[row["start_sequence"]:row["end_sequence"]+1]
    received = [event["packet_id"] for event in row["server_events"] if event["event"] == "packet_out_received"]
    assert len(received) == len(set(received)), received
    if mode in ("pre_cancelled", "queued_cancelled"):
        assert 20000 not in received and 99999 in received
    if mode == "graceful":
        assert received == [1]
        assert any(event["event"] == "stream_final_ok" for event in row["server_events"])
    if mode == "graceful_timeout":
        assert received == [1]
        assert not any(event["event"] == "stream_final_ok" for event in row["server_events"])
    row["received_packet_ids"] = received
    assert row["driver_exit_code"] == 0
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--adapters", nargs="+", default=["sdk", "finsy"])
    parser.add_argument("--modes", nargs="+")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    trace = Trace(args.output / "rpc.jsonl")
    rows = []
    try:
        with (args.output / "observations.jsonl").open("w") as file:
            for adapter in args.adapters:
                modes = args.modes or (["pre_cancelled", "queued_cancelled", "inflight_cancelled",
                                       "immediate", "graceful", "graceful_timeout"] if adapter == "sdk"
                                      else ["inflight_cancelled", "immediate"])
                for mode in modes:
                    for repetition in range(1, args.repetitions + 1):
                        row = trial(adapter, mode, repetition, trace, args.output)
                        rows.append(row)
                        file.write(json.dumps(row) + "\n")
                        file.flush()
                        print(f"{adapter} {mode}: {repetition}/{args.repetitions}", flush=True)
    finally:
        trace.close()
    configurations = {adapter: args.modes or (
        ["pre_cancelled", "queued_cancelled", "inflight_cancelled", "immediate", "graceful", "graceful_timeout"]
        if adapter == "sdk" else ["inflight_cancelled", "immediate"]) for adapter in args.adapters}
    snapshot(args.output, {"kind": "cancellation", "trials": len(rows), "repetitions": args.repetitions,
                           "configurations": configurations,
                           "payload_bytes": PAYLOAD_BYTES, "backpressure_probe_seconds": .2,
                           "recovery_observation_seconds": 60})


if __name__ == "__main__":
    main()
