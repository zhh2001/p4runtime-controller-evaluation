import argparse
from collections import Counter,defaultdict
import csv
import gzip
import hashlib
import itertools
import json
from pathlib import Path

from finsy.proto import p4r
from google.protobuf import json_format

from audit import check_run,statistics
from common import PAPER,ROOT,decode_entries
from run import check_ready,check_wire


def source_check(output):
    manifest=json.loads((output/"manifest.json").read_text())
    for name,digest in manifest["files_sha256"].items():
        assert hashlib.sha256((output/"source"/name).read_bytes()).hexdigest()==digest,name
    expected=manifest["files_sha256"]["experiments/performance/fixtures/control.p4info.txt"]
    assert hashlib.sha256((ROOT/"fixtures/control.p4info.txt").read_bytes()).hexdigest()==expected
    return manifest


def analyze_capacity(output,manifest):
    rows=json.loads((output/"observations.json").read_text())
    assert len(rows)==manifest["repetitions"]*5
    for repetition in range(1,manifest["repetitions"]+1):
        for count in (256,4096,16384,32768):
            row=next(row for row in rows if row["repetition"]==repetition and row.get("capacity")==count)
            assert all(item["outcome"]["rpc_code"]==0 for item in row["writes"])
            items=json.loads((output/f"state-{repetition}-{count}.json").read_text())
            entries=[json_format.ParseDict(item,p4r.TableEntry()) for item in items]
            assert decode_entries(entries)=={key:1 for key in range(1,count+1)}
        overflow=next(row for row in rows if row["repetition"]==repetition and "overflow" in row)
        assert overflow["overflow"]["rpc_code"]==2
        assert len(overflow["overflow"]["details"])==1
        assert overflow["overflow"]["details"][0]["code"]==2
        assert overflow["delete"]["rpc_code"]==overflow["replace"]["rpc_code"]==0
        assert overflow["final_count"]==32768 and overflow["state_matches"]
    processes=json.loads((output/"target-processes.json").read_text())
    assert all("exit_code" in process for process in processes)
    return {"repetitions":manifest["repetitions"],"max_verified_entries":32768,
            "overflow_rejected":manifest["repetitions"],"overflow_rpc_code":2,
            "overflow_update_code":2,"slot_reuse_verified":manifest["repetitions"],
            "owned_target_processes_stopped":len(processes)}


def analyze_workloads(output,manifest):
    briefs=[json.loads(line) for line in (output/"observations.jsonl").read_text().splitlines()]
    assert len(briefs)==manifest["phases"]
    groups=defaultdict(list)
    summaries=[]
    all_records=0
    for brief in briefs:
        row=json.loads((output/brief["detail_file"]).read_text())
        assert all(row[key]==value for key,value in brief.items() if key!="detail_file")
        groups[row["profile"],row["repetition"],row["n"],row["batch"],row["concurrency"]].append(row)
    expected=set(itertools.product(manifest["profiles"],range(1,manifest["repetitions"]+1),
                                   [tuple(config) for config in manifest["configurations"]]))
    assert {(profile,repetition,(n,b,c)) for profile,repetition,n,b,c in groups}==expected
    failed=0
    oracle_writes=0
    peak=0
    for key,phases in groups.items():
        expected_state={}
        steps=[("insert","measurement")]
        if manifest["inject_failure"]:steps.append(("modify","fault-probe"))
        for operation in ("modify","read_one","read_all"):
            steps.extend([(operation,"warmup"),(operation,"measurement")])
        assert [(row["operation"],row["phase"]) for row in phases]==steps
        for row in phases:
            check_ready(row["ready"])
            expected_state,observed_peak=check_run(row,expected_state,
                allow_unapplied_rpc_failure=row["phase"]=="fault-probe")
            peak=max(peak,observed_peak)
            if manifest["kind"]=="oracle":
                check_wire(row,row["oracle_events"])
                oracle_writes+=sum(event["event"]=="write" for event in row["oracle_events"])
                if row["phase"]=="fault-probe":
                    assert row["failed_calls"]==1 and row["unknown_updates"]==row["batch"]
                    assert sum(event["event"]=="injected_unavailable" for event in row["oracle_events"])==1
            if row["phase"]!="fault-probe":
                assert row["failed_calls"]==row["unknown_updates"]==0
            failed+=row["failed_calls"]
            all_records+=row["attempts"]
            resources=row["resources"]
            for process in ("client","target"):
                before,after=resources["before"][process],resources["after"][process]
                if before:
                    assert after["user_ticks"]>=before["user_ticks"]
                    assert after["system_ticks"]>=before["system_ticks"]
                    assert after["monotonic_ns"]>=before["monotonic_ns"]
            state=row["state"]
            packed=(output/state["file"]).read_bytes()
            assert hashlib.sha256(packed).hexdigest()==state["gzip_sha256"]
            raw=gzip.decompress(packed)
            assert hashlib.sha256(raw).hexdigest()==state["protobuf_sha256"]
            response=p4r.ReadResponse.FromString(raw)
            assert decode_entries([entity.table_entry for entity in response.entities])==expected_state
            assert len(response.entities)==state["entries"]==row["n"]
            summary={"profile":row["profile"],"repetition":row["repetition"],"n":row["n"],
                     "batch":row["batch"],"concurrency":row["concurrency"],"operation":row["operation"],
                     "phase":row["phase"],"elapsed_ns":row["elapsed_ns"],**statistics(row)}
            summaries.append(summary)
    with (output/"phase-summary.csv").open("w",newline="") as file:
        writer=csv.DictWriter(file,fieldnames=list(summaries[0]))
        writer.writeheader();writer.writerows(summaries)
    drivers=json.loads((output/"drivers.json").read_text())
    assert len(drivers)==len(manifest["profiles"])*manifest["repetitions"]
    assert all(row["exit_code"]==0 for row in drivers)
    assert not any("DATA RACE" in path.read_text() for path in output.glob("*.stderr"))
    if manifest["kind"]!="oracle":
        processes=json.loads((output/"target-processes.json").read_text())
        assert all("exit_code" in process for process in processes)
    measurements=sum(row["phase"]=="measurement" for row in summaries)
    warmups=sum(row["phase"]=="warmup" for row in summaries)
    return {"configurations":len(groups),"phases":len(summaries),"measurement_phases":measurements,
            "warmup_phases":warmups,"recorded_attempts":all_records,"failed_calls":failed,
            "injected_failures":len(groups) if manifest["inject_failure"] else 0,
            "oracle_write_requests_verified":oracle_writes,"max_outstanding_rpcs":peak,
            "full_state_checks":len(summaries),"driver_processes_exited":len(drivers),
            "race_reports":0}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("outputs",nargs="+",type=Path)
    args=parser.parse_args()
    for output in args.outputs:
        manifest=source_check(output)
        result=analyze_capacity(output,manifest) if manifest["kind"]=="capacity" else analyze_workloads(output,manifest)
        result["source_snapshot_verified"]=True
        sources=[Path(__file__),ROOT/"audit.py",ROOT/"common.py",ROOT/"run.py"]
        hashes={}
        for source in sources:
            name=str(source.relative_to(PAPER))
            data=source.read_bytes()
            target=output/"analysis-source"/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(data)
            hashes[name]=hashlib.sha256(data).hexdigest()
        (output/"analysis-manifest.json").write_text(json.dumps({"files_sha256":hashes},indent=2)+"\n")
        (output/"verification.json").write_text(json.dumps(result,indent=2)+"\n")
        print(json.dumps({"output":str(output),**result}),flush=True)


if __name__=="__main__":main()
