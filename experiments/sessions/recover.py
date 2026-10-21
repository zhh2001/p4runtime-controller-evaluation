"""Ungated recovery trials, keeping native library reconnect policies."""

import time
import grpc

from common import ROOT,Trace,Target,Relay,Session,SessionDriver,entry,message,status_record


class UngatedRelay(Relay):
    def cut(self,stream_id,mode):
        with self.lock:
            self.cuts[stream_id]=mode
            call=self.calls[stream_id]
        event=self.trace.add('fault_injected',stream_id=stream_id,mode=mode)
        call.cancel()
        return event


def expected_entry(port,marker):
    value=entry(1,marker)
    value.action.action.params[0].value=bytes([port])
    return message(value)


def trial(adapter,mode,repetition,trace,output):
    tag=f'{adapter}-{mode}-{repetition}'
    started=trace.add('trial_started',kind='recovery',adapter=adapter,mode=mode,repetition=repetition)
    target=Target(output/(tag+'-target'));relay=None;driver=None;observer=None
    row=dict(adapter=adapter,mode=mode,repetition=repetition,start_sequence=started['sequence'],recovered=False,observation_window_seconds=60)
    try:
        target.start();relay=UngatedRelay(target.address,trace)
        seed=Session(relay.address,arbitrate=True)
        try:seed.install()
        finally:seed.close()
        driver=SessionDriver(adapter,relay.address,output/(tag+'.stderr'))
        observer=Session(relay.address)
        row['initial']=driver.command('write');assert row['initial']['rpc_code']==0
        row['initial_entries']=[message(v) for v in observer.read()]
        assert row['initial_entries']==[expected_entry(1,b'session-write')]
        time.sleep(2.1)
        current=max(v['stream_id'] for v in trace.rows[started['sequence']:] if v['event']=='arbitration_response' and v['response']['arbitration'].get('status',{}).get('code',0)==0)
        if mode=='restart':
            fault=trace.add('target_restart',stream_id=current,adapter=adapter)
            target.stop();target.start()
            ready=trace.add('target_ready',stream_id=current)
            row['target_ready_sequence']=ready['sequence']
        else:fault=relay.cut(current,mode)
        row['fault_sequence']=fault['sequence']
        deadline=fault['monotonic_ns']/1e9+60
        try:
            arbitration=trace.wait(lambda v:v['event']=='arbitration_response' and v['stream_id']>current and v['sequence']>fault['sequence'] and v['response']['arbitration'].get('status',{}).get('code',0)==0,
                                   max(0,deadline-time.monotonic()))
        except TimeoutError:
            row.update(censored=True,censor_ns=60000000000)
            return row
        row['arbitration_sequence']=arbitration['sequence']
        while True:
            primary=driver.command('state')
            if primary['is_primary']:break
            if time.monotonic()>=deadline:
                row.update(censored=True,censor_ns=60000000000)
                return row
            time.sleep(.005)
        row['primary']=primary
        observed=trace.add('primary_observed',state=primary)
        row['primary_observed_sequence']=observed['sequence']
        observer.election.CopyFrom(__import__('google.protobuf.json_format',fromlist=['ParseDict']).ParseDict(arbitration['response']['arbitration']['election_id'],type(observer.election)()))
        if mode=='restart':
            try:
                pipeline=observer.pipeline()
                missing=not pipeline.config.HasField('p4info')
                row['pipeline_before_restore']=status_record(None)
            except grpc.RpcError as error:
                missing=error.code()==grpc.StatusCode.FAILED_PRECONDITION
                row['pipeline_before_restore']=status_record(error)
            assert missing,row['pipeline_before_restore']
            row['pipeline_missing']=True
            restore=trace.add('application_restore_started')
            row['restore_started_sequence']=restore['sequence']
            observer.install()
            row['entries_after_install']=[message(v) for v in observer.read()]
            assert row['entries_after_install']==[]
            row['final_write']=driver.command('restore-final')
        else:
            row['pipeline_missing']=False
            row['preserved_entries']=[message(v) for v in observer.read()]
            assert row['preserved_entries']==row['initial_entries']
            row['final_write']=driver.command('modify-final')
        assert row['final_write']['rpc_code']==0,row['final_write']
        writes=[v for v in trace.rows[started['sequence']:] if v['event']=='write_end']
        row['writes']=writes
        last=writes[-1];row['successful_write_sequence']=last['sequence']
        row['final_entries']=[message(v) for v in observer.read()]
        assert row['final_entries']==[expected_entry(2,b'recovered')]
        verified=trace.add('final_state_verified')
        row.update(recovered=True,censored=False,verified_sequence=verified['sequence'],
            fault_to_arbitration_ns=arbitration['monotonic_ns']-fault['monotonic_ns'],
            fault_to_primary_observed_ns=observed['monotonic_ns']-fault['monotonic_ns'],
            fault_to_successful_write_ns=last['monotonic_ns']-fault['monotonic_ns'],
            fault_to_verified_state_ns=verified['monotonic_ns']-fault['monotonic_ns'])
        return row
    finally:
        if observer:observer.close()
        if driver:
            driver.close();row['driver_exit_code']=driver.process.returncode
            row['driver_records']=driver.records
        if relay:
            assert relay.gate.is_set(),'arbitration gate was changed'
            relay.close()
        target.stop();row['target_processes']=target.history
        row['end_sequence']=trace.add('trial_completed',kind='recovery',adapter=adapter,mode=mode)['sequence']
