from collections import Counter, defaultdict
import math


def check_run(row,expected,allow_unapplied_rpc_failure=False):
    records=row["records"]
    n,batch,concurrency=row["n"],row["batch"],row["concurrency"]
    operation=row["operation"]
    write=operation in ("insert","modify")
    assert records and row["attempts"]==len(records)
    assert row["rpc_calls"]==sum(record["call_made"] for record in records)
    assert row["successful_calls"]==sum(record["rpc_code"]==0 for record in records)
    assert row["failed_calls"]==sum(record["rpc_code"]!=0 for record in records)
    assert row["attempts"]==row["successful_calls"]+row["failed_calls"]
    for field in ("ok_updates","failed_updates","unknown_updates"):
        assert row[field]==sum(record[field] for record in records),field
    assert row["end_ns"]-row["start_ns"]==row["elapsed_ns"]>0
    if row["requested_seconds"]>0:
        assert row["scheduled_stop_ns"]-row["start_ns"]==int(row["requested_seconds"]*1e9)
        assert row["end_ns"]>=row["scheduled_stop_ns"]
    parts=defaultdict(list)
    events=[]
    for record in records:
        worker,iteration=record["worker"],record["iteration"]
        assert 0<=worker<concurrency
        parts[worker].append(record)
        assert row["start_ns"]<=record["admission_ns"]<=record["start_ns"]<=record["build_end_ns"]<=record["end_ns"]<=row["end_ns"]
        if row["requested_seconds"]>0:
            assert record["admission_ns"]<row["scheduled_stop_ns"]
        if record["call_made"]:
            events.extend([(record["build_end_ns"],1),(record["end_ns"],-1)])
        if write:
            first=((worker+iteration*concurrency)%(n//batch))*batch+1
            port=1 if operation=="insert" else 2
            if operation=="modify" and row["requested_seconds"]>0:
                port=2-(iteration//(n//batch//concurrency)%2)
            assert (record["first"],record["count"],record["port"])==(first,batch,port)
            assert record["ok_updates"]+record["failed_updates"]+record["unknown_updates"]==batch
        elif operation=="read_one":
            first=1+(worker+iteration*concurrency)%n
            assert record["first"]==first and record["count"]==1
            if record["rpc_code"]==0:
                assert (record["read_count"],record["key_sum"],record["port_sum"])==(1,first,expected[first])
        else:
            assert operation=="read_all" and record["first"]==0
            if record["rpc_code"]==0:
                assert (record["read_count"],record["key_sum"],record["port_sum"])==(n,n*(n+1)//2,sum(expected.values()))
    for worker,part in parts.items():
        assert [record["iteration"] for record in part]==list(range(len(part)))
        assert all(after["admission_ns"]>=before["end_ns"] for before,after in zip(part,part[1:]))
    outstanding,peak=0,0
    for _,delta in sorted(events):
        outstanding+=delta
        peak=max(peak,outstanding)
        assert outstanding>=0
    assert outstanding==0 and peak<=concurrency
    if row["requested_seconds"]==0:
        expected_calls=n//batch if write else concurrency
        assert row["attempts"]==expected_calls
    updated=dict(expected)
    for record in sorted(records,key=lambda record:record["end_ns"]):
        if record["rpc_code"]!=0:
            assert allow_unapplied_rpc_failure and record["rpc_code"]==14
            continue
        if write:
            for key in range(record["first"],record["first"]+record["count"]):
                if operation=="insert":assert key not in updated
                else:assert key in updated
                updated[key]=record["port"]
    return updated,peak


def percentile(values,q):
    assert values and 0<=q<=1
    ordered=sorted(values)
    return ordered[max(0,math.ceil(q*len(ordered))-1)]


def statistics(row):
    elapsed=row["elapsed_ns"]/1e9
    values=[record["end_ns"]-record["start_ns"] for record in row["records"]]
    successful=[record for record in row["records"] if record["rpc_code"]==0]
    return {"attempts":len(values),"p50_ns":percentile(values,.5),
            "p95_ns":percentile(values,.95),"p99_ns":percentile(values,.99),
            "successful_rpcs_per_second":len(successful)/elapsed,
            "confirmed_updates_per_second":row["ok_updates"]/elapsed,
            "failed_calls":row["failed_calls"],"unknown_updates":row["unknown_updates"],
            "read_entities_per_second":sum(record["read_count"] for record in successful)/elapsed}
