"""Native Python table-entry adapters with unchanged source checkouts."""

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import queue
import sys
import time

import grpc

OUTPUT=sys.stdout


def emit(value):print(json.dumps(value),file=OUTPUT,flush=True)


def failure(row,error):
    row.update(error=str(error),error_type=type(error).__name__,rpc_code=2)
    code=getattr(error,'code',None)
    if callable(code):row['rpc_code']=code().value[0]
    elif code is not None:row['rpc_code']=int(code)


class Shell:
    def __init__(self,address):
        import p4runtime_sh.shell as sh
        self.sh=sh;sh.setup(device_id=1,grpc_addr=address,election_id=(0,1),verbose=False)
        original=sh.client.stub
        class Deadlines:
            def Write(self,request):return original.Write(request,timeout=5)
            def Read(self,request):return original.Read(request,timeout=5)
            def __getattr__(self,name):return getattr(original,name)
        sh.client.stub=Deadlines()

    def execute(self,c):
        sh=self.sh;entry=sh.TableEntry(c['table'])
        for item in c['matches']:
            entry.match[item['name']]='0x'+item['value']+('&&&0x'+item['mask'] if item['kind']=='ternary' else '')
        entry.is_default=c['default']
        entry.priority=c['priority'];entry.idle_timeout_ns=c['ttl']
        if c['action_kind']=='direct':
            action=sh.Action(c['action'])
            for p in c['params']:action[p['name']]='0x'+p['value']
            entry.action=action
        elif c['action_kind']=='member':entry.member_id=c['reference']
        elif c['action_kind']=='group':entry.group_id=c['reference']
        getattr(entry,c['update'].lower())()

    def close(self):self.sh.teardown()


async def finsy_main(args):
    import finsy as fy
    from finsy.p4client import P4Client
    from finsy.proto import p4r,p4r_grpc
    channel=grpc.insecure_channel(args.address);stub=p4r_grpc.P4RuntimeStub(channel);requests=queue.Queue()
    def iterator():
        while (item:=requests.get()) is not None:yield item
    stream=stub.StreamChannel(iterator())
    requests.put(p4r.StreamMessageRequest(arbitration=p4r.MasterArbitrationUpdate(device_id=1,election_id=p4r.Uint128(low=1))))
    assert next(stream).arbitration.status.code==0
    schema=fy.P4Schema(Path(args.p4info));client=P4Client(args.address);await client.open(schema=schema)
    emit(dict(ready=True,profile='finsy'))
    try:
        for line in sys.stdin:
            c=json.loads(line)
            if c['op']=='close':break
            row=dict(id=c['id'],token=c['token'],stage='completed',rpc_code=0);started=time.monotonic_ns()
            try:
                match={v['name']:('0x'+v['value'],'0x'+v['mask']) if v['kind']=='ternary' else '0x'+v['value'] for v in c['matches']}
                kind=c['action_kind'];action=None
                if kind=='direct':action=fy.P4TableAction(c['action'],**{p['name']:'0x'+p['value'] for p in c['params']})
                elif kind=='member':action=fy.P4IndirectAction(member_id=c['reference'])
                elif kind=='group':action=fy.P4IndirectAction(group_id=c['reference'])
                entry=fy.P4TableEntry(c['table'],match=fy.P4TableMatch(match),action=action,is_default_action=c['default'],priority=c['priority'],idle_timeout_ns=c['ttl'])
                await client.request(p4r.WriteRequest(device_id=1,election_id=p4r.Uint128(low=1),updates=[p4r.Update(type=getattr(p4r.Update,c['update']),entity=entry.encode(schema))]))
            except Exception as error:failure(row,error)
            row['elapsed_ns']=time.monotonic_ns()-started;emit(row)
    finally:
        await client.close();requests.put(None);stream.cancel();channel.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--address',required=True);parser.add_argument('--profile',required=True);parser.add_argument('--p4info',required=True)
    args=parser.parse_args()
    with contextlib.redirect_stdout(sys.stderr):
        if args.profile=='finsy':asyncio.run(finsy_main(args));return
        driver=Shell(args.address);emit(dict(ready=True,profile='shell'))
        try:
            for line in sys.stdin:
                c=json.loads(line)
                if c['op']=='close':break
                row=dict(id=c['id'],token=c['token'],stage='completed',rpc_code=0);started=time.monotonic_ns()
                try:driver.execute(c)
                except Exception as error:failure(row,error)
                row['elapsed_ns']=time.monotonic_ns()-started;emit(row)
        finally:driver.close()


if __name__=='__main__':main()
