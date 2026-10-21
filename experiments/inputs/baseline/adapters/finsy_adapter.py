import asyncio
import json
import os
from pathlib import Path
import sys

import finsy as fy
from finsy import p4entity
from finsy.p4client import P4Client, P4ClientError
from finsy.proto import p4r
from google.protobuf import json_format


async def main():
    address, p4info = sys.argv[1:]
    case = json.load(sys.stdin)
    result = {"case_id": case["id"], "stage": "local_rejected", "exposed_results": None}
    schema = fy.P4Schema(Path(p4info))
    marker = f'{case["suite"]}|{case["id"]}|{os.environ["P4_ADAPTER"]}'.encode()
    try:
        if case["suite"] == "E1":
            matches = {}
            for match in case["matches"]:
                value = int(match["value"], 16)
                if match["kind"] == "lpm":
                    value = (value, match["prefix"])
                elif match["kind"] == "ternary":
                    value = (value, int(match["mask"], 16))
                elif match["kind"] == "range":
                    value = (value, int(match["high"], 16))
                matches[match["field"]] = value
            entity = fy.P4TableEntry(case["table"], match=fy.P4TableMatch(matches),
                                     action=fy.P4TableAction(case["action"], **{name: int(value,16) for name,value in case["params"].items()}),
                                     priority=case["priority"], metadata=marker)
            updates = p4entity.encode_updates([+entity], schema)
            result["wire"] = json_format.MessageToDict(updates[0].entity.table_entry, preserving_proto_field_name=True)
        else:
            updates = p4entity.encode_updates([
                +fy.P4TableEntry("MyIngress.t_exact", match=fy.P4TableMatch({"s.ingress_port": i+1}),
                                  action=fy.P4TableAction("MyIngress.forward", port=1), metadata=marker)
                for i in range(case["count"])], schema)
    except Exception as exc:
        result.update(error=str(exc), error_type=type(exc).__name__)
        return result
    client = P4Client(address)
    try:
        await client.open(schema=schema)
    except Exception as exc:
        result.update(stage="setup_error", error=str(exc), error_type=type(exc).__name__)
        return result
    try:
        await client.request(p4r.WriteRequest(device_id=1, election_id=p4r.Uint128(low=1), updates=updates))
        result.update(stage="request_completed", call_succeeded=True, rpc_code=0)
    except P4ClientError as exc:
        result.update(stage="request_completed", call_succeeded=False, rpc_code=int(exc.code), error=str(exc),
                      exposed_results=[{"index": i, "code": int(item.canonical_code)} for i,item in exc.details.items()])
    except Exception as exc:
        result.update(stage="status_parse_error", call_succeeded=False, error=str(exc), error_type=type(exc).__name__)
    finally:
        await client.close()
    return result


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
