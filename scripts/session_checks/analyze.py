import argparse
from collections import Counter, defaultdict
import csv
from functools import lru_cache
import hashlib
import itertools
import json
from pathlib import Path
import statistics

from .model import entry, message, verify_snapshot, expected_entry


def recovery(row,events):
    assert row['driver_exit_code']==0
    assert row['initial']['rpc_code']==0
    assert row['initial_entries']==[expected_entry(1,b'session-write')]
    phase=[v for k,v in events.items() if row['start_sequence']<=k<=row['end_sequence']]
    assert not any(v['event']=='arbitration_gate_released' for v in phase)
    assert all('exit_code' in p for p in row['target_processes'])
    fault=events[row['fault_sequence']]
    assert fault['event']==('target_restart' if row['mode']=='restart' else 'fault_injected')
    if not row['recovered']:
        assert row['censored'] and row['censor_ns']==60000000000
        return
    assert not row['censored'] and row['primary']['is_primary']
    arb=events[row['arbitration_sequence']];primary=events[row['primary_observed_sequence']]
    write=events[row['successful_write_sequence']];verified=events[row['verified_sequence']]
    assert arb['event']=='arbitration_response' and arb['response']['arbitration'].get('status',{}).get('code',0)==0
    assert fault['sequence']<arb['sequence']<primary['sequence']<write['sequence']<verified['sequence']
    for key,event in [('arbitration',arb),('primary_observed',primary),('successful_write',write),('verified_state',verified)]:
        assert row['fault_to_'+key+'_ns']==event['monotonic_ns']-fault['monotonic_ns']>=0
    assert row['final_write']['rpc_code']==0
    assert row['final_entries']==[expected_entry(2,b'recovered')]
    assert len(row['writes'])==2 and [w['rpc_code'] for w in row['writes']]==[0,0]
    assert [v for v in phase if v['event']=='write_end']==row['writes']
    if row['mode']=='restart':
        assert row['pipeline_missing'] and row['entries_after_install']==[]
        assert events[row['target_ready_sequence']]['event']=='target_ready'
        assert events[row['restore_started_sequence']]['event']=='application_restore_started'
    else:assert not row['pipeline_missing'] and row['preserved_entries']==row['initial_entries']


@lru_cache(maxsize=None)
def payload_digest(number, size):
    payload = b"P4EXP001" + number.to_bytes(8, "big") + bytes(size-16)
    return hashlib.sha256(payload).hexdigest()


def sampled_peak(row):
    return max(sample["rss_kib"] for sample in row["rss_samples"])


def mastership(row, events):
    assert row["initial"]["rpc_code"] == row["final_write"]["rpc_code"] == 0
    assert row["backup"]["is_primary"] is False
    assert row["primary"]["is_primary"] is True
    assert row["final_entries"] == [message(entry(1, b"session-write"))]
    sequences = [row[key] for key in ("takeover_sequence", "demotion_sequence",
                                     "backup_observed_sequence", "higher_closed_sequence")]
    assert sequences == sorted(set(sequences))
    assert events[sequences[1]]["response"]["arbitration"]["status"]["code"] == 6
    assert row["demotion_to_observation_ns"] >= 0
    if row["adapter"] == "sdk":
        assert row["backup_write"]["not_primary"] and row["backup_write_rpcs"] == 0
        assert [item["rpc_code"] for item in row["writes"]] == [0, 0]
    else:
        assert row["backup_write"]["rpc_code"] == 7 and row["backup_write_rpcs"] == 1
        assert [item["rpc_code"] for item in row["writes"]] == [0, 7, 0]


def cancellation(row, events):
    mode, adapter = row["mode"], row["adapter"]
    assert row["driver_exit_code"] == 0
    actual = [event["packet_id"] for event in row["server_events"]
              if event["event"] == "packet_out_received"]
    assert row["received_packet_ids"] == actual and len(set(actual)) == len(actual)
    for event in row["server_events"]:
        assert events[event["sequence"]] == event
        if event["event"] == "packet_out_received":
            size = 2*1024*1024 if mode in ("queued_cancelled","inflight_cancelled") and event["packet_id"] < 1000 else 64
            assert event["size"] == size
            assert event["sha256"] == payload_digest(event["packet_id"],size)
            assert event["metadata"] == [{"metadata_id":1,"value":"AQ=="}]
    if mode in ("pre_cancelled", "queued_cancelled"):
        assert row["cancelled_send"]["cancelled"]
        assert 20000 not in actual and actual[-1] == 99999
        assert row["stream_count"] == 1
        assert row["final_state"]["is_primary"]
        if mode == "queued_cancelled":
            assert row["blocked_send"]["rpc_code"] == 0
            assert row["backpressure_sequence"] < row["end_sequence"]
    elif mode == "inflight_cancelled":
        assert row["cancelled_send"]["cancelled"]
        assert isinstance(row["recovered"], bool)
        if row["recovered"]:
            assert row["stream_count"] >= 2 and 99999 in actual
            assert row["final_state"]["is_primary"]
    else:
        assert row["send"]["rpc_code"] == 0
        if adapter == "sdk":
            assert row["close"]["is_primary"] is False
        else:
            assert row["close"]["is_up"] is False
        if mode == "graceful":
            assert actual == [1]
            assert row["close"]["rpc_code"] == 0
            assert row["request_eof_sequence"] < row["final_status_released_sequence"]
            final = [event for event in row["server_events"] if event["event"] == "stream_final_ok"]
            assert len(final) == 1 and final[0]["sequence"] > row["final_status_released_sequence"]
            assert row["close"]["monotonic_ns"] >= final[0]["monotonic_ns"]
        elif mode == "graceful_timeout":
            assert actual == [1] and row["close"]["deadline"]
            assert not any(event["event"] == "stream_final_ok" for event in row["server_events"])
        else:
            assert mode == "immediate" and actual == []
    close = next(item for item in row["driver_records"]
                 if item.get("kind") == "reply" and item.get("op") == "close")
    if adapter == "finsy":
        assert close["tasks_after_close"] == 0
    assert not any("DATA RACE" in str(record) for record in row["driver_records"])


def consumers(row, events):
    final = row["final"]
    sent = row["sent"]
    phase = [event for sequence, event in events.items()
             if row["start_sequence"] <= sequence <= row["end_sequence"]]
    emitted = [event["packet_id"] for event in phase if event["event"] == "packet_in_emit"]
    assert emitted == list(range(sent))
    assert row["baseline"]["arrived"] == row["baseline"]["processed"] == []
    assert final["is_primary"] is False
    arrived, processed = final["arrived"], final["processed"]
    assert arrived == sorted(set(arrived))
    assert processed == sorted(set(processed))
    assert set(processed).issubset(arrived) and set(arrived).issubset(range(sent))
    assert row["delivered"] == len(processed)
    assert row["application_dropped"] == final["application_dropped"] == len(arrived)-len(processed)
    assert row["not_delivered_to_iterator"] == sent-len(arrived)
    assert row["not_processed"] == sent-len(processed)
    assert row["not_processed"] == row["application_dropped"]+row["not_delivered_to_iterator"]
    assert final["in_progress"] == final["queue_depth"] == 0
    assert len(final["arrival_times_ns"]) == len(arrived)
    assert len(final["completion_times_ns"]) == len(processed)
    assert final["arrival_times_ns"] == sorted(final["arrival_times_ns"])
    assert final["completion_times_ns"] == sorted(final["completion_times_ns"])
    marker = events[row["marker_sequence"]]
    observation = events[row["observation_sequence"]]
    assert marker["event"] == "arbitration_emit" and marker["code"] == 6
    assert row["marker_to_observed_backup_ns"] == observation["client_monotonic_ns"]-marker["monotonic_ns"] >= 0
    assert row["rss_samples"] and row["peak_sampled_rss_kib"] in {
        item["rss_kib"] for item in row["rss_samples"]}
    assert row["baseline_rss_kib"] == row["rss_samples"][0]["rss_kib"]
    assert row["driver_exit_code"] == 0
    if row["adapter"] == "sdk" and row["path"] == "native":
        assert processed == list(range(sent))
    if row["adapter"] == "finsy":
        assert row["close_record"]["tasks_after_close"] == 0


def analyze(output):
    manifest = verify_snapshot(output)
    rows = [json.loads(line) for line in (output/"observations.jsonl").read_text().splitlines()]
    events = {row["sequence"]: row for row in map(json.loads, (output/"rpc.jsonl").read_text().splitlines())}
    assert len(rows) == manifest["trials"]
    kind = manifest["kind"]
    repetitions = range(1, manifest["repetitions"]+1)
    if kind == 'recovery':
        expected=set(itertools.product(manifest['adapters'],manifest['plan']['recovery_modes'],repetitions))
        assert {(r['adapter'],r['mode'],r['repetition']) for r in rows}==expected
        assert len(rows)==len(expected)
        for row in rows:recovery(row,events)
        result=dict(trials=len(rows),session_recoveries=sum(r['recovered'] for r in rows),
                    right_censored=sum(r['censored'] for r in rows),driver_processes_exited=len(rows),
                    application_pipeline_restores=sum(r['recovered'] and r['mode']=='restart' for r in rows),
                    no_arbitration_gate=True,final_entries_verified=sum(r['recovered'] for r in rows))
        with (output/'recovery-trials.csv').open('w',newline='') as f:
            fields=['adapter','mode','repetition','recovered','censored','fault_to_arbitration_ns','fault_to_primary_observed_ns','fault_to_successful_write_ns','fault_to_verified_state_ns']
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
            for row in rows:writer.writerow({k:row.get(k,'') for k in fields})
    elif kind == "mastership":
        expected = set(itertools.product(manifest['adapters'], repetitions))
        assert {(row["adapter"], row["repetition"]) for row in rows} == expected
        assert len(rows) == len(expected)
        for row in rows:
            mastership(row, events)
        processes = json.loads((output/"target-processes.json").read_text())
        assert all("exit_code" in process for process in processes)
        result = {"trials": len(rows), "backup_states_verified": len(rows),
                  "primary_restorations": len(rows), "final_entries_verified": len(rows),
                  "owned_target_processes_stopped": len(processes)}
    elif kind == "cancellation":
        configurations = manifest.get("configurations", {
            "sdk":["pre_cancelled","queued_cancelled","inflight_cancelled","immediate","graceful","graceful_timeout"],
            "finsy":["inflight_cancelled","immediate"]})
        expected = {(adapter, mode, repetition) for adapter, modes in configurations.items()
                    for mode in modes for repetition in repetitions}
        assert {(row["adapter"], row["mode"], row["repetition"]) for row in rows} == expected
        assert len(rows) == len(expected)
        for row in rows:
            cancellation(row, events)
        logs = list(output.glob("*.stderr"))
        assert not any("DATA RACE" in path.read_text() for path in logs)
        inflight = [row for row in rows if row["mode"] == "inflight_cancelled"]
        result = {"trials": len(rows), "cancelled_requests_absent": sum(
            row["mode"] in ("pre_cancelled", "queued_cancelled") for row in rows),
            "inflight_cancellations": len(inflight),
            "inflight_session_recoveries": sum(row["recovered"] for row in inflight),
            "graceful_final_status_verified": sum(row["mode"] == "graceful" for row in rows),
            "graceful_deadlines_verified": sum(row["mode"] == "graceful_timeout" for row in rows),
            "driver_processes_exited": len(rows), "race_reports": 0}
        with (output/"outcomes.csv").open("w",newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["adapter","mode","trials","inflight_recoveries","received_after_immediate_close"])
            groups = defaultdict(list)
            for row in rows:
                groups[row["adapter"],row["mode"]].append(row)
            for (adapter,mode), group in sorted(groups.items()):
                writer.writerow([adapter,mode,len(group),sum(row.get("recovered",False) for row in group),
                                 sum(len(row["received_packet_ids"]) for row in group) if mode=="immediate" else ""])
    elif kind == "consumers":
        expected = set(itertools.product(manifest["adapters"],manifest["paths"],manifest["delays_ms"],repetitions))
        assert {(row["adapter"],row["path"],row["delay_ms"],row["repetition"]) for row in rows} == expected
        assert len(rows) == len(expected)
        for row in rows:
            consumers(row,events)
        assert not any("DATA RACE" in path.read_text() for path in output.glob("*.stderr"))
        result = {"trials": len(rows), "packets_emitted": sum(row["sent"] for row in rows),
                  "packet_accounting_verified": True,"arbitration_observations":len(rows),
                  "driver_processes_exited":len(rows),"race_reports":0,
                  "rss_summaries_recomputed":sum(sampled_peak(row)!=row["peak_sampled_rss_kib"] for row in rows)}
        evaluations = [{"adapter":row["adapter"],"path":row["path"],"delay_ms":row["delay_ms"],
                        "repetition":row["repetition"],"recorded_peak_kib":row["peak_sampled_rss_kib"],
                        "peak_from_all_samples_kib":sampled_peak(row)} for row in rows]
        (output/"rss-evaluation.jsonl").write_text("".join(json.dumps(item)+"\n" for item in evaluations))
        groups = defaultdict(list)
        for row in rows:
            groups[row["adapter"],row["path"],row["delay_ms"]].append(row)
        with (output/"consumer-summary.csv").open("w",newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["adapter","path","delay_ms","trials","processed_min","processed_max",
                             "not_processed_min","not_processed_max","backup_observation_ms_min",
                             "backup_observation_ms_median","backup_observation_ms_max","peak_sampled_rss_kib_median"])
            for (adapter,path,delay), group in sorted(groups.items()):
                delays = [row["marker_to_observed_backup_ns"]/1e6 for row in group]
                writer.writerow([adapter,path,delay,len(group),min(row["delivered"] for row in group),
                                 max(row["delivered"] for row in group),min(row["not_processed"] for row in group),
                                 max(row["not_processed"] for row in group),min(delays),statistics.median(delays),
                                 max(delays),statistics.median(sampled_peak(row) for row in group)])
    else:
        raise ValueError(kind)
    result["analysis_fixture_snapshot_verified"] = True
    (output/"verification.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps({"results":str(output),**result}))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("results",type=Path,nargs="+")
    args = parser.parse_args()
    for output in args.results:
        analyze(output)
