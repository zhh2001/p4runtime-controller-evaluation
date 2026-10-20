import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
from pathlib import Path
import sys
import threading
import time

import grpc
from google.protobuf import json_format

OUTPUT = sys.stdout
TABLE, FIELD, ACTION = "MyIngress.forwarding", "h.key.value", "MyIngress.forward"


def emit(row):
    print(json.dumps(row),file=OUTPUT,flush=True)


class Shell:
    def __init__(self,address):
        import p4runtime_sh.shell as sh
        from p4.v1 import p4runtime_pb2 as p4r
        sh.setup(device_id=1,grpc_addr=address,election_id=(0,1),verbose=False)
        original = sh.client.stub
        class DeadlineStub:
            def Write(self,request):
                return original.Write(request,timeout=10)
            def Read(self,request):
                return original.Read(request,timeout=10)
            def __getattr__(self,name):
                return getattr(original,name)
        sh.client.stub = DeadlineStub()
        self.sh,self.p4r=sh,p4r

    def table_entry(self,key,port):
        item=self.sh.TableEntry(TABLE)
        item.match[FIELD]=str(key)
        item.action=self.sh.Action(ACTION)
        item.action["port"]=str(port)
        item.metadata=b"E2"
        return item

    def sample(self,key,port):
        return self.table_entry(key,port).msg()

    def build(self,operation,first,count,port):
        if operation in ("insert","modify"):
            request=self.p4r.WriteRequest()
            for key in range(first,first+count):
                update=request.updates.add(type=self.p4r.Update.INSERT if operation=="insert" else self.p4r.Update.MODIFY)
                update.entity.table_entry.CopyFrom(self.table_entry(key,port).msg())
            return request
        item=self.sh.TableEntry(TABLE)
        if operation=="read_one":
            item.match[FIELD]=str(first)
        return item

    def call(self,operation,prepared):
        if operation in ("insert","modify"):
            self.sh.client.write(prepared)
            return []
        return list(prepared.read())

    def totals(self,items):
        count,key_sum,port_sum=0,0,0
        for entity in items:
            item=entity.msg()
            if item.is_default_action:continue
            count+=1
            key_sum+=int.from_bytes(item.match[0].exact.value,"big")
            port_sum+=int.from_bytes(item.action.action.params[0].value,"big")
        return count,key_sum,port_sum

    def close(self):
        self.sh.teardown()


class Finsy:
    async def open(self,address,info):
        import finsy as fy
        from finsy import p4entity
        self.fy,self.p4entity=fy,p4entity
        ready=asyncio.Event()
        async def on_ready(switch):ready.set()
        self.switch=fy.Switch("performance",address,options=fy.SwitchOptions(
            p4info=info,device_id=1,initial_election_id=1,ready_handler=on_ready))
        self.task=asyncio.create_task(self.switch.run())
        await asyncio.wait_for(ready.wait(),15)
        assert self.switch.is_primary and self.switch.is_up

    def table_entry(self,key,port):
        return self.fy.P4TableEntry(TABLE,match=self.fy.P4TableMatch({FIELD:key}),
            action=self.fy.P4TableAction(ACTION,port=port),metadata=b"E2")

    def sample(self,key,port):
        return self.p4entity.encode_updates([+self.table_entry(key,port)],self.switch.p4info)[0].entity.table_entry

    def build(self,operation,first,count,port):
        if operation in ("insert","modify"):
            entities=[self.table_entry(key,port) for key in range(first,first+count)]
            return [+item if operation=="insert" else ~item for item in entities]
        return self.fy.P4TableEntry(TABLE,match=self.fy.P4TableMatch(
            {FIELD:first} if operation=="read_one" else {}))

    async def call(self,operation,prepared):
        if operation in ("insert","modify"):
            await self.switch.write(prepared)
            return []
        return [item async for item in self.switch.read([prepared])]

    def totals(self,items):
        count,key_sum,port_sum=0,0,0
        for item in items:
            if item.is_default_action:continue
            count+=1
            key_sum+=int(next(iter(item.match.values())))
            port_sum+=int(item.action.args["port"])
        return count,key_sum,port_sum

    async def close(self):
        self.task.cancel()
        try:await self.task
        except asyncio.CancelledError:pass


def prepare_row(command,worker,iteration):
    n,batch,concurrency=command["n"],command["batch"],command["concurrency"]
    operation=command["operation"]
    chunks=n//batch
    first=((worker+iteration*concurrency)%chunks)*batch+1
    count,port=batch,1 if operation=="insert" else 2
    if operation=="modify" and command["seconds"]>0:
        port=2-(iteration//(chunks//concurrency)%2)
    elif operation=="read_one":
        first,count,port=1+(worker+iteration*concurrency)%n,1,0
    elif operation=="read_all":
        first,count,port=0,n,0
    return {"worker":worker,"iteration":iteration,"first":first,"count":count,"port":port,
            "start_ns":time.monotonic_ns(),"call_made":False,"rpc_code":0,"read_count":0,
            "key_sum":0,"port_sum":0,"ok_updates":0,"failed_updates":0,"unknown_updates":0}


def failure(row,error,write):
    row["error"]=str(error)
    row["error_type"]=type(error).__name__
    code=getattr(error,"code",None)
    if callable(code):
        value=code()
        row["rpc_code"]=value.value[0] if isinstance(value,grpc.StatusCode) else int(value)
    elif code is not None:
        row["rpc_code"]=int(code)
    elif hasattr(error,"grpc_error"):
        row["rpc_code"]=error.grpc_error.code().value[0]
    else:
        row["rpc_code"]=2
    if write:
        if not row["call_made"]:
            row["failed_updates"]=row["count"]
        else:
            row["unknown_updates"]=row["count"]
            details=getattr(error,"details",None)
            pairs=details.items() if isinstance(details,dict) else getattr(error,"errors",[])
            valid=[(index,item) for index,item in pairs
                   if 0<=index<row["count"] and 0<int(item.canonical_code)<=16]
            if len({index for index,_ in valid})==len(valid):
                row["failed_updates"]=len(valid)
                row["unknown_updates"]-=len(valid)


def finish_row(row,items,backend,write,error):
    if error is not None:
        failure(row,error,write)
    elif write:
        row["ok_updates"]=row["count"]
    else:
        row["read_count"],row["key_sum"],row["port_sum"]=backend.totals(items)


def valid(command):
    n,batch,concurrency=command["n"],command["batch"],command["concurrency"]
    return (0<n<=32768 and batch>0 and n%batch==0 and concurrency>0 and
            n//batch%concurrency==0 and command["seconds"]>=0 and
            command["operation"] in ("insert","modify","read_one","read_all"))


def summary(command,started,stop,ended,records):
    records.sort(key=lambda row:row["start_ns"])
    return {"operation":command["operation"],"phase":command["phase"],"n":command["n"],
            "batch":command["batch"],"concurrency":command["concurrency"],
            "requested_seconds":command["seconds"],"start_ns":started,"scheduled_stop_ns":stop,
            "end_ns":ended,"elapsed_ns":ended-started,"attempts":len(records),
            "rpc_calls":sum(row["call_made"] for row in records),
            "successful_calls":sum(row["rpc_code"]==0 for row in records),
            "failed_calls":sum(row["rpc_code"]!=0 for row in records),
            "ok_updates":sum(row["ok_updates"] for row in records),
            "failed_updates":sum(row["failed_updates"] for row in records),
            "unknown_updates":sum(row["unknown_updates"] for row in records),"records":records}


def finite_limit(command):
    return command["n"]//command["batch"] if command["operation"] in ("insert","modify") else command["concurrency"]


def run_shell(command,backend):
    if not valid(command):return {"fatal":"invalid workload partition"}
    ready=threading.Barrier(command["concurrency"]+1)
    gate=threading.Event()
    timing={}
    write=command["operation"] in ("insert","modify")
    def worker(number):
        records=[]
        ready.wait()
        gate.wait()
        iteration=0
        while True:
            admission=time.monotonic_ns()
            if command["seconds"]>0:
                if admission>=timing["stop"]:break
            elif number+iteration*command["concurrency"]>=finite_limit(command):break
            row=prepare_row(command,number,iteration)
            row["admission_ns"]=admission
            items,error=[],None
            try:
                prepared=backend.build(command["operation"],row["first"],row["count"],row["port"])
                row["build_end_ns"]=time.monotonic_ns()
                row["call_made"]=True
                items=backend.call(command["operation"],prepared)
            except Exception as caught:
                error=caught
            row.setdefault("build_end_ns",time.monotonic_ns())
            row["end_ns"]=time.monotonic_ns()
            finish_row(row,items,backend,write,error)
            records.append(row)
            iteration+=1
        return records
    with ThreadPoolExecutor(max_workers=command["concurrency"]) as executor:
        futures=[executor.submit(worker,number) for number in range(command["concurrency"])]
        ready.wait()
        timing["start"]=time.monotonic_ns()
        timing["stop"]=timing["start"]+int(command["seconds"]*1e9)
        gate.set()
        parts=[future.result() for future in futures]
        ended=time.monotonic_ns()
    return summary(command,timing["start"],timing["stop"],ended,[row for part in parts for row in part])


async def run_finsy(command,backend):
    if not valid(command):return {"fatal":"invalid workload partition"}
    gate=asyncio.Event()
    timing={}
    write=command["operation"] in ("insert","modify")
    async def worker(number):
        records=[]
        await gate.wait()
        iteration=0
        while True:
            admission=time.monotonic_ns()
            if command["seconds"]>0:
                if admission>=timing["stop"]:break
            elif number+iteration*command["concurrency"]>=finite_limit(command):break
            row=prepare_row(command,number,iteration)
            row["admission_ns"]=admission
            items,error=[],None
            try:
                prepared=backend.build(command["operation"],row["first"],row["count"],row["port"])
                row["build_end_ns"]=time.monotonic_ns()
                row["call_made"]=True
                items=await backend.call(command["operation"],prepared)
            except Exception as caught:
                error=caught
            row.setdefault("build_end_ns",time.monotonic_ns())
            row["end_ns"]=time.monotonic_ns()
            finish_row(row,items,backend,write,error)
            records.append(row)
            iteration+=1
        return records
    tasks=[asyncio.create_task(worker(number)) for number in range(command["concurrency"])]
    await asyncio.sleep(0)
    timing["start"]=time.monotonic_ns()
    timing["stop"]=timing["start"]+int(command["seconds"]*1e9)
    gate.set()
    parts=await asyncio.gather(*tasks)
    return summary(command,timing["start"],timing["stop"],time.monotonic_ns(),[row for part in parts for row in part])


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--profile",choices=["finsy","shell"],required=True)
    parser.add_argument("--address",required=True)
    parser.add_argument("--p4info",type=Path,required=True)
    args=parser.parse_args()
    if args.profile=="finsy":
        backend=Finsy()
        await backend.open(args.address,args.p4info)
    else:
        backend=Shell(args.address)
    try:
        samples=[json_format.MessageToDict(backend.sample(key,port),preserving_proto_field_name=True)
                 for key,port in [(1,1),(256,2),(0xffffffff,511)]]
        emit({"ready":True,"profile":args.profile,"wire_samples":samples,"rpc_deadline_seconds":10})
        while True:
            line=await asyncio.to_thread(sys.stdin.readline) if args.profile=="finsy" else sys.stdin.readline()
            if not line:break
            command=json.loads(line)
            if command["op"]=="close":
                break
            result=await run_finsy(command,backend) if args.profile=="finsy" else run_shell(command,backend)
            emit(result)
    finally:
        if args.profile=="finsy":await backend.close()
        else:backend.close()
    emit({"closed":True})


if __name__=="__main__":
    with contextlib.redirect_stdout(sys.stderr):
        asyncio.run(main())
