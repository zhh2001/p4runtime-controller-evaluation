import asyncio
import json
from pathlib import Path
import sys

import finsy as fy
from finsy.p4client import P4ClientError


async def main():
    address,info=sys.argv[1:]
    ready=asyncio.Event()
    async def on_ready(switch):ready.set()
    switch=fy.Switch("pilot",address,options=fy.SwitchOptions(p4info=Path(info),device_id=1,initial_election_id=1,ready_handler=on_ready))
    task=asyncio.create_task(switch.run())
    await asyncio.wait_for(ready.wait(),15)
    def emit(row):print(json.dumps(row),flush=True)
    emit({"op":"ready","is_primary":switch.is_primary,"is_up":switch.is_up})
    try:
        while True:
            line=await asyncio.to_thread(sys.stdin.readline)
            if not line:return
            command=json.loads(line)
            row={"op":command["op"]}
            try:
                if command["op"]=="await-primary":
                    async def wait():
                        while not(switch.is_primary and switch.is_up):await asyncio.sleep(.005)
                    await asyncio.wait_for(wait(),60)
                elif command["op"] in ("write","modify","modify-final","restore-final"):
                    entry=fy.P4TableEntry("MyIngress.t_exact",match=fy.P4TableMatch({"s.ingress_port":1}),action=fy.P4TableAction("MyIngress.forward",port=2 if command["op"].endswith("-final") else 1),metadata=b"recovered" if command["op"].endswith("-final") else b"session-write")
                    await switch.write([+entry if command["op"] in ("write","restore-final") else ~entry])
                elif command["op"] not in ("state","close"):
                    raise ValueError("unknown command")
                row["rpc_code"]=0
            except P4ClientError as error:
                row.update(rpc_code=int(error.code),error=str(error))
            except Exception as error:
                row.update(error=str(error),error_type=type(error).__name__)
            row.update(is_primary=switch.is_primary,is_up=switch.is_up)
            emit(row)
            if command["op"]=="close":return
    finally:
        task.cancel()
        try:await task
        except asyncio.CancelledError:pass


if __name__=="__main__":asyncio.run(main())
