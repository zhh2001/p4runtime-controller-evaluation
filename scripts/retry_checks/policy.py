"""Validate the status envelope before associating failures with a retry subset."""

import base64
from finsy.proto import p4r, rpc_status


def complete_codes(row,count,profile):
    if row['rpc_code']==0:return [0]*count,'success'
    if row['rpc_code']!=2:return None,'rpc_outcome_unknown'
    if profile=='sdk':
        items=row.get('native_results')
        if items is None:return None,'sdk_rejected_incomplete_status'
        if len(items)!=count or [v['index'] for v in items]!=list(range(count)):return None,'invalid_native_indices'
        return [v['code'] for v in items],'sdk_public_results'
    # Other clients expose a failure map or an unparsed status. This application
    # adds an envelope guard. It is not attributed to those libraries.
    raw=row.get('raw_status')
    if not raw:return None,'application_missing_status'
    try:
        status=rpc_status.Status.FromString(base64.b64decode(raw,validate=True))
        if status.code!=2 or len(status.details)!=count:return None,'application_incomplete_status'
        codes=[]
        for detail in status.details:
            item=p4r.Error()
            if not detail.Unpack(item) or not 0<=item.canonical_code<=16:return None,'application_invalid_status'
            codes.append(item.canonical_code)
        if not any(codes):return None,'application_no_failure'
        if profile in ('finsy','shell'):
            public=row.get('native_results')
            if public is None:return None,'native_parse_error'
            expected=[dict(index=i,code=code) for i,code in enumerate(codes) if code]
            if public!=expected:return None,'public_results_disagree'
        return codes,'native_failure_map_guarded' if profile in ('finsy','shell') else 'application_decoded_status'
    except Exception:return None,'application_status_parse_error'
