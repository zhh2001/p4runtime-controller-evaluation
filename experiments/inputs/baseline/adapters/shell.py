import contextlib
import json
import os
import sys

from google.protobuf import json_format
from p4.v1 import p4runtime_pb2 as p4r
import p4runtime_sh.shell as sh
from p4runtime_sh.p4runtime import P4RuntimeWriteException


def main():
    address, _p4info = sys.argv[1:]
    case = json.load(sys.stdin)
    result = {"case_id": case["id"], "stage": "setup_error", "exposed_results": None}
    marker = f'{case["suite"]}|{case["id"]}|{os.environ["P4_ADAPTER"]}'.encode()
    with contextlib.redirect_stdout(sys.stderr):
        try:
            sh.setup(device_id=1, grpc_addr=address, election_id=(0,1), verbose=False)
        except Exception as exc:
            result.update(error=str(exc), error_type=type(exc).__name__)
            return result
        try:
            result["stage"] = "local_rejected"
            if case["suite"] == "E1":
                entry = sh.TableEntry(case["table"])
                for match in case["matches"]:
                    value = "0x" + match["value"]
                    if match["kind"] == "lpm":
                        value += "/" + str(match["prefix"])
                    elif match["kind"] == "ternary":
                        value += "&&&0x" + match["mask"]
                    elif match["kind"] == "range":
                        value += "..0x" + match["high"]
                    entry.match[match["field"]] = value
                entry.action = sh.Action(case["action"])
                for name,value in case["params"].items():
                    entry.action[name] = "0x" + value
                entry.priority = case["priority"]
                entry.metadata = marker
                result["wire"] = json_format.MessageToDict(entry.msg(), preserving_proto_field_name=True)
                entry.insert()
            else:
                request = p4r.WriteRequest()
                for i in range(case["count"]):
                    entity = sh.TableEntry("MyIngress.t_exact")
                    entity.match["s.ingress_port"] = str(i+1)
                    entity.action = sh.Action("MyIngress.forward")
                    entity.action["port"] = "1"
                    entity.metadata = marker
                    update = request.updates.add(type=p4r.Update.INSERT)
                    update.entity.table_entry.CopyFrom(entity.msg())
                result["stage"] = "write_started"
                sh.client.write(request)
            result.update(stage="request_completed", call_succeeded=True, rpc_code=0)
        except P4RuntimeWriteException as exc:
            result.update(stage="request_completed", call_succeeded=False, rpc_code=2, error=str(exc),
                          exposed_results=[{"index": i, "code": item.canonical_code} for i,item in exc.errors])
        except Exception as exc:
            result.update(error=str(exc), error_type=type(exc).__name__)
            if case["suite"] == "E4":
                result["stage"] = "status_parse_error"
        finally:
            sh.teardown()
    return result


if __name__ == "__main__":
    print(json.dumps(main()))
