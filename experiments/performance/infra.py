import base64
import json
from pathlib import Path
import socket
import subprocess
import threading
import time

import grpc
from finsy.proto import p4r, rpc_status


def status_record(error):
    if error is None:
        return {"rpc_code":0,"details":[],"trailing_metadata":[]}
    result={"rpc_code":error.code().value[0],"error":error.details(),"details":[],"trailing_metadata":[]}
    for key,value in error.trailing_metadata() or ():
        result["trailing_metadata"].append({"key":key,"value":base64.b64encode(value).decode() if isinstance(value,bytes) else value,"binary":isinstance(value,bytes)})
        if key=="grpc-status-details-bin":
            status=rpc_status.Status.FromString(value)
            for index,detail in enumerate(status.details):
                item=p4r.Error()
                if detail.Unpack(item):
                    result["details"].append({"index":index,"code":item.canonical_code,"message":item.message,"space":item.space,"vendor_code":item.code})
    return result


class Trace:
    def __init__(self,path):
        self.file=open(path,"w")
        self.rows=[]
        self.condition=threading.Condition()

    def add(self,event,**fields):
        with self.condition:
            row={"sequence":len(self.rows),"monotonic_ns":time.monotonic_ns(),"event":event,**fields}
            self.rows.append(row)
            self.file.write(json.dumps(row)+"\n")
            self.file.flush()
            self.condition.notify_all()
            return row

    def wait(self,predicate,timeout=60):
        deadline=time.monotonic()+timeout
        with self.condition:
            while True:
                found=next((row for row in self.rows if predicate(row)),None)
                if found is not None:return found
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError("trace event was not observed")
                self.condition.wait(min(remaining,0.1))

    def close(self):self.file.close()


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


class Target:
    def __init__(self,directory):
        self.directory=Path(directory)
        self.directory.mkdir(exist_ok=True,parents=True)
        self.port=unused_port()
        self.thrift=unused_port()
        while self.thrift==self.port:self.thrift=unused_port()
        self.address=f"127.0.0.1:{self.port}"
        self.process=None
        self.generation=0
        self.history=[]

    def start(self):
        assert self.process is None
        self.generation+=1
        self.ipc=Path(f"/tmp/p4eval-{id(self)}-{self.generation}.ipc")
        command=["simple_switch_grpc","--no-p4","--device-id","1","--thrift-port",str(self.thrift),"--notifications-addr","ipc://"+str(self.ipc),"--log-console","-L","info","--","--grpc-server-addr",self.address,"--cpu-port","255"]
        self.log=open(self.directory/f"target-{self.generation}.log","w")
        self.process=subprocess.Popen(command,stdout=self.log,stderr=subprocess.STDOUT)
        self.history.append({"generation":self.generation,"pid":self.process.pid,"command":command,"start_ns":time.monotonic_ns()})
        channel=grpc.insecure_channel(self.address)
        try:
            grpc.channel_ready_future(channel).result(timeout=10)
        finally:channel.close()
        if self.process.poll() is not None:raise RuntimeError("owned BMv2 process exited")

    def stop(self):
        if self.process is None:return
        self.process.terminate()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill();self.process.wait(timeout=5)
        self.history[-1].update(stop_ns=time.monotonic_ns(),exit_code=self.process.returncode)
        self.process=None
        self.log.close()
        self.ipc.unlink(missing_ok=True)
