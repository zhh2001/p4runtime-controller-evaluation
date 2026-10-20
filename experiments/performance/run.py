import argparse
from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path
import random
import threading
import time

from finsy.proto import p4r
from google.protobuf import json_format

from audit import check_run
from common import (ACTION,CONFIGURATIONS,Control,Driver,MARKER,PROFILES,ROOT,TABLE,
                    Target,Trace,decode_entries,snapshot)
from oracle import Oracle


def check_ready(row):
    assert row["rpc_deadline_seconds"]==10
    assert len(row["wire_samples"])==3
    for sample,(key,port) in zip(row["wire_samples"],[(1,1),(256,2),(0xffffffff,511)]):
        item=json_format.ParseDict(sample,p4r.TableEntry())
        assert item.table_id==TABLE.preamble.id and len(item.match)==1
        field=item.match[0]
        assert field.field_id==TABLE.match_fields[0].id
        assert field.WhichOneof("field_match_type")=="exact"
        assert int.from_bytes(field.exact.value,"big")==key
        assert 0<len(field.exact.value)<=4
        assert item.action.action.action_id==ACTION.preamble.id and len(item.action.action.params)==1
        param=item.action.action.params[0]
        assert param.param_id==ACTION.params[0].id and int.from_bytes(param.value,"big")==port
        assert 0<len(param.value)<=2
        assert item.metadata==MARKER and not item.is_default_action and item.priority==0


def process(pid):
    if pid is None:return None
    text=Path(f"/proc/{pid}/stat").read_text()
    fields=text[text.rfind(")")+2:].split()
    rss=None
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):rss=int(line.split()[1])
    return {"monotonic_ns":time.monotonic_ns(),"user_ticks":int(fields[11]),
            "system_ticks":int(fields[12]),"rss_kib":rss}


def execute(driver,target_pid,command):
    stop=threading.Event()
    samples=[]
    before={"client":process(driver.process.pid),"target":process(target_pid)}
    def sample():
        while not stop.is_set():
            samples.append({"client":process(driver.process.pid),"target":process(target_pid)})
            stop.wait(.02)
    sampler=threading.Thread(target=sample,daemon=True)
    sampler.start()
    try:
        row=driver.command(**command)
    finally:
        stop.set()
        sampler.join(timeout=2)
        assert not sampler.is_alive()
    after={"client":process(driver.process.pid),"target":process(target_pid)}
    row["resources"]={"before":before,"after":after,"rss_samples":samples,
                      "clock_ticks_per_second":os.sysconf("SC_CLK_TCK"),
                      "scope":"parent command interval, including result serialization and IPC"}
    return row


def state_snapshot(control,expected,path):
    entries=control.read()
    state=decode_entries(entries)
    assert state==expected,{"missing":list(set(expected)-set(state))[:8],
                           "extra":list(set(state)-set(expected))[:8],
                           "different":[key for key in state if key in expected and state[key]!=expected[key]][:8]}
    response=p4r.ReadResponse(entities=[p4r.Entity(table_entry=item) for item in entries])
    data=response.SerializeToString(deterministic=True)
    packed=gzip.compress(data,mtime=0)
    path.write_bytes(packed)
    return {"file":path.name,"entries":len(entries),"state_matches":True,
            "protobuf_sha256":hashlib.sha256(data).hexdigest(),
            "gzip_sha256":hashlib.sha256(packed).hexdigest()}


def check_wire(row,events):
    operation=row["operation"]
    if operation in ("insert","modify"):
        actual=[]
        kind="INSERT" if operation=="insert" else "MODIFY"
        for event in events:
            if event["event"]!="write":continue
            request=event["request"]
            keys=[];ports=[]
            for update in request["updates"]:
                assert update["type"]==kind
                item=json_format.ParseDict(update["entity"]["table_entry"],p4r.TableEntry())
                values=decode_entries([item])
                key,port=next(iter(values.items()))
                keys.append(key);ports.append(port)
            assert keys==list(range(keys[0],keys[0]+len(keys))) and len(set(ports))==1
            actual.append((keys[0],len(keys),ports[0]))
        expected=[(record["first"],record["count"],record["port"]) for record in row["records"] if record["call_made"]]
    else:
        actual=[]
        for event in events:
            if event["event"]!="read":continue
            item=json_format.ParseDict(event["request"]["entities"][0]["table_entry"],p4r.TableEntry())
            actual.append(int.from_bytes(item.match[0].exact.value,"big") if item.match else 0)
        expected=[record["first"] for record in row["records"] if record["call_made"]]
    assert Counter(actual)==Counter(expected)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--repetitions",type=int,default=2)
    parser.add_argument("--warmup",type=float,default=.05)
    parser.add_argument("--seconds",type=float,default=.15)
    parser.add_argument("--profiles",nargs="+",choices=PROFILES,default=PROFILES)
    parser.add_argument("--configurations",nargs="+",help="N:B:C")
    parser.add_argument("--smoke",action="store_true")
    parser.add_argument("--oracle",action="store_true")
    parser.add_argument("--inject-failure",action="store_true")
    args=parser.parse_args()
    assert args.warmup>0 and args.seconds>0 and args.repetitions>0
    assert not args.inject_failure or args.oracle
    configurations=[tuple(map(int,text.split(":"))) for text in args.configurations] if args.configurations else (
        [(64,16,1)] if args.smoke else CONFIGURATIONS)
    assert all(len(config)==3 and 0<config[0]<=32768 and config[1]>0 and config[2]>0
               and config[0]%config[1]==0 and config[0]//config[1]%config[2]==0 for config in configurations)
    args.output.mkdir(parents=True,exist_ok=False)
    trace=Trace(args.output/"oracle.jsonl") if args.oracle else None
    target=None if args.oracle else Target(args.output/"target")
    oracle=Oracle(trace) if args.oracle else None
    rows=[];drivers=[];order=[]
    rng=random.Random(20261017)
    try:
        if target:target.start()
        address=oracle.address if oracle else target.address
        target_pid=target.process.pid if target else None
        with (args.output/"observations.jsonl").open("w") as output:
            for repetition in range(1,args.repetitions+1):
                profiles=list(args.profiles)
                rng.shuffle(profiles)
                order.append(profiles)
                for profile in profiles:
                    seed=Control(address,True)
                    try:seed.install()
                    finally:seed.close()
                    driver=Driver(profile,address,args.output/f"{profile}-{repetition}.stderr")
                    control=Control(address)
                    try:
                        (args.output/f"ready-{profile}-{repetition}.json").write_text(json.dumps(driver.ready)+"\n")
                        check_ready(driver.ready)
                        for n,batch,concurrency in configurations:
                            control.install()
                            expected={}
                            commands=[("insert","measurement",0)]
                            if args.inject_failure:commands.append(("modify","fault-probe",0))
                            for operation in ("modify","read_one","read_all"):
                                commands.extend([(operation,"warmup",args.warmup),(operation,"measurement",args.seconds)])
                            for operation,phase,seconds in commands:
                                number=len(rows)
                                command={"op":"run","operation":operation,"n":n,"batch":batch,
                                         "concurrency":concurrency,"seconds":seconds,"phase":phase}
                                if phase=="fault-probe":oracle.fail_modify=True
                                offset=len(trace.rows) if trace else 0
                                row=execute(driver,target_pid,command)
                                events=list(trace.rows[offset:]) if trace else []
                                row.update(profile=profile,repetition=repetition,index=number,
                                           ready=driver.ready,oracle_events=events,command=command)
                                raw_path=args.output/f"raw-phase-{number}.json"
                                raw_path.write_text(json.dumps(row)+"\n")
                                try:
                                    expected,peak=check_run(row,expected,allow_unapplied_rpc_failure=phase=="fault-probe")
                                    if oracle:check_wire(row,events)
                                    if phase=="fault-probe":
                                        assert row["failed_calls"]==1 and row["unknown_updates"]==batch
                                        assert sum(event["event"]=="injected_unavailable" for event in events)==1
                                    else:assert row["failed_calls"]==row["unknown_updates"]==0
                                    state=state_snapshot(control,expected,args.output/f"state-{number}.pb.gz")
                                except Exception as error:
                                    (args.output/"failure.json").write_text(json.dumps({
                                        "profile":profile,"repetition":repetition,"command":command,
                                        "raw_file":raw_path.name,"exception":type(error).__name__,"error":str(error)},indent=2)+"\n")
                                    raise
                                row["peak_outstanding_rpcs"]=peak
                                row["state"]=state
                                (args.output/f"phase-{number}.json").write_text(json.dumps(row)+"\n")
                                brief={key:value for key,value in row.items() if key not in ("records","resources","oracle_events")}
                                brief["detail_file"]=f"phase-{number}.json"
                                output.write(json.dumps(brief)+"\n");output.flush()
                                rows.append(brief)
                            print(f"{profile} {repetition}: N={n}, B={batch}, C={concurrency}",flush=True)
                    finally:
                        control.close()
                        driver.close()
                        drivers.append({"profile":profile,"repetition":repetition,"pid":driver.process.pid,
                                        "exit_code":driver.process.returncode})
                        assert driver.process.returncode==0
    finally:
        if oracle:oracle.close()
        if target:
            target.stop()
            (args.output/"target-processes.json").write_text(json.dumps(target.history,indent=2)+"\n")
        if trace:trace.close()
        (args.output/"drivers.json").write_text(json.dumps(drivers,indent=2)+"\n")
    snapshot(args.output,{"kind":"oracle" if args.oracle else "direct_bmv2_preflight",
             "repetitions":args.repetitions,"profiles":args.profiles,"profile_order":order,
             "configurations":configurations,"warmup_seconds":args.warmup,
             "measurement_seconds":args.seconds,"phases":len(rows),"inject_failure":args.inject_failure,
             "random_seed":20261017,"rpc_deadline_seconds":10,"protocol_revision":2})
    print(json.dumps({"phases":len(rows),"output":str(args.output)}),flush=True)


if __name__=="__main__":main()
