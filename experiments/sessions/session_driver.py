import argparse
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import threading
import time

import grpc

from infra import ROOT, Relay, Session, Target, Trace, message, status_record

REPOSITORY=ROOT.parents[1]
FINSY=REPOSITORY/".runtime/finsy/bin/python"


class Driver:
    def __init__(self,adapter,address,log):
        self.log=open(log,"w")
        self.output=Path(log).with_suffix('.driver.jsonl').open('w',buffering=1)
        self.records=[]
        command=[__import__("os").environ.get("P4EXPERIMENT_SESSION_BINARY",str(ROOT/"bin/session-sdk")),"--address",address,"--p4info",str(ROOT/"fixtures/keys.p4info.txt")] if adapter=="sdk" else [str(FINSY),str(ROOT/"session_finsy.py"),address,str(ROOT/"fixtures/keys.p4info.txt")]
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,bufsize=1)
        self.queue=queue.Queue()
        def read():
            for line in self.process.stdout:
                self.output.write(line)
                try:
                    row=json.loads(line);self.records.append(row);self.queue.put(row)
                except ValueError:self.queue.put({"unparsed":line})
            self.queue.put({"process_ended":True})
        self.thread=threading.Thread(target=read,daemon=True);self.thread.start()
        try:self.ready=self.receive(20)
        except BaseException:
            self.close()
            raise
        if self.ready.get("error") or not self.ready.get("is_primary"):
            self.close();raise RuntimeError(f"driver not ready: {self.ready}")

    def receive(self,timeout=65):
        row=self.queue.get(timeout=timeout)
        if row.get("process_ended") or "unparsed" in row:raise RuntimeError(str(row))
        return row

    def command(self,op):
        self.process.stdin.write(json.dumps({"op":op})+"\n");self.process.stdin.flush()
        row=self.receive()
        assert row["op"]==op,row
        return row

    def close(self):
        if self.process.poll() is None:
            try:self.command("close")
            except (BrokenPipeError,RuntimeError,queue.Empty):pass
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(timeout=5)
        self.thread.join(timeout=2)
        self.process.stdin.close();self.process.stdout.close()
        assert not self.thread.is_alive()
        self.log.close()
        self.output.close()

