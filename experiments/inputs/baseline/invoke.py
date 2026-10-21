import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run_child(adapter,command,case,address,repetition,logs):
    command=command+([address,str(ROOT/"fixtures/keys.p4info.txt")] if adapter in ("finsy","shell") else ["--address",address,"--p4info",str(ROOT/"fixtures/keys.p4info.txt")])
    env=dict(os.environ,P4_ADAPTER=f"{adapter}@{repetition}")
    started=time.monotonic_ns()
    try:
        process=subprocess.run(command,input=json.dumps(case),capture_output=True,text=True,env=env,timeout=6)
        row=json.loads(process.stdout) if process.returncode==0 else {"stage":"process_error","exit_code":process.returncode,"stdout":process.stdout}
        stderr=process.stderr
    except subprocess.TimeoutExpired as exc:
        row={"stage":"timeout","timeout_seconds":6}
        stderr=exc.stderr or b""
    if isinstance(stderr,bytes):stderr=stderr.decode(errors="replace")
    if stderr:(logs/f'{repetition}-{case["suite"]}-{case["id"]}-{adapter}.stderr').write_text(stderr)
    row.update(adapter=adapter,repetition=repetition,case_id=case["id"],suite=case["suite"],category=case["category"],elapsed_ns=time.monotonic_ns()-started)
    return row
