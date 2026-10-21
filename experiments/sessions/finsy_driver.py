import argparse
import asyncio
import json
from pathlib import Path
import time

import finsy as fy
from finsy.p4client import P4ClientError


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--p4info", type=Path, required=True)
    parser.add_argument("--path", choices=["native", "queued"], default="native")
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--capacity", type=int, default=50)
    args = parser.parse_args()
    def emit(row):
        print(json.dumps({**row, "monotonic_ns": time.monotonic_ns()}), flush=True)
    ready = asyncio.Event()
    async def on_ready(switch):
        ready.set()
    switch = fy.Switch("stream-pilot", args.address, options=fy.SwitchOptions(
        p4info=args.p4info, device_id=1, initial_election_id=1, ready_handler=on_ready))
    session = asyncio.create_task(switch.run())
    await asyncio.wait_for(ready.wait(), 15)
    arrivals, processed, arrival_times, completion_times = [], [], [], []
    dropped, in_progress = 0, 0
    application = asyncio.Queue(args.capacity)
    async def process(number):
        nonlocal in_progress
        in_progress += 1
        if args.delay_ms:
            await asyncio.sleep(args.delay_ms / 1000)
        processed.append(number)
        completion_times.append(time.monotonic_ns())
        in_progress -= 1
    async def packets():
        nonlocal dropped
        async for packet in switch.read_packets(queue_size=50):
            number = int.from_bytes(packet.payload[8:16], "big")
            arrivals.append(number)
            arrival_times.append(time.monotonic_ns())
            if args.path == "native":
                await process(number)
            else:
                try:
                    application.put_nowait(number)
                except asyncio.QueueFull:
                    dropped += 1
    async def worker():
        while True:
            await process(await application.get())
    packet_task = asyncio.create_task(packets())
    worker_task = asyncio.create_task(worker()) if args.path == "queued" else None
    await asyncio.sleep(0)
    jobs = {}
    async def send(job, size, pre_cancelled=False):
        payload = b"P4EXP001" + job.to_bytes(8, "big") + bytes(size - 16)
        emit({"kind": "send_started", "job": job})
        try:
            if pre_cancelled:
                raise asyncio.CancelledError()
            await switch.write([fy.P4PacketOut(payload, egress_port=1)])
            emit({"kind": "send_result", "job": job, "rpc_code": 0})
        except asyncio.CancelledError:
            emit({"kind": "send_result", "job": job, "cancelled": True})
        except P4ClientError as error:
            emit({"kind": "send_result", "job": job, "rpc_code": int(error.code), "error": str(error)})
        except Exception as error:
            emit({"kind": "send_result", "job": job, "error": str(error), "error_type": type(error).__name__})
        finally:
            jobs.pop(job, None)
    async def close_session(job):
        session.cancel()
        try:
            await session
        except asyncio.CancelledError:
            pass
        emit({"kind": "close_result", "job": job, "rpc_code": 0, "is_primary": switch.is_primary,
              "is_up": switch.is_up})
    emit({"kind": "ready", "is_primary": switch.is_primary, "path": args.path, "delay_ms": args.delay_ms})
    import sys
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            command = json.loads(line)
            row = {"kind": "reply", "id": command["id"], "op": command["op"]}
            if command["op"] in ("state", "stats"):
                row.update(is_primary=switch.is_primary, is_up=switch.is_up, arrived=arrivals.copy(),
                           processed=processed.copy(), arrival_times_ns=arrival_times.copy(),
                           completion_times_ns=completion_times.copy(), application_dropped=dropped,
                           in_progress=in_progress, queue_depth=application.qsize(),
                           tasks=len(asyncio.all_tasks()))
            elif command["op"] == "start-send":
                job = command["job"]
                jobs[job] = asyncio.create_task(send(job, command["size"], command.get("pre_cancelled", False)))
            elif command["op"] == "cancel":
                if task := jobs.get(command["job"]):
                    task.cancel()
            elif command["op"] == "start-close":
                if command.get("graceful"):
                    row["fatal"] = "no public graceful-close API in this profile"
                else:
                    asyncio.create_task(close_session(command["job"]))
            elif command["op"] == "close":
                break
            else:
                row["fatal"] = "unknown command"
            emit(row)
    finally:
        pending = list(jobs.values()) + [packet_task, session]
        if worker_task:
            pending.append(worker_task)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    if line:
        emit({"kind": "reply", "id": command["id"], "op": "close",
              "tasks_after_close": len([task for task in asyncio.all_tasks() if task is not asyncio.current_task()])})


if __name__ == "__main__":
    asyncio.run(main())
