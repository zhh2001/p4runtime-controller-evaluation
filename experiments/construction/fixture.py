"""Shared P4Info-only workload for offline TableEntry construction."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def integer(value):
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big").hex()


def prepare():
    folder = ROOT / "fixtures"
    folder.mkdir(exist_ok=True)
    (folder / "construct.p4info.txt").write_text('''pkg_info { name: "construction" version: "1" arch: "v1model" }
tables {
  preamble { id: 33554433 name: "MyIngress.forwarding" alias: "forwarding" }
  match_fields { id: 1 name: "h.key.value" bitwidth: 32 match_type: EXACT }
  action_refs { id: 16777217 } size: 32768
}
tables {
  preamble { id: 33554434 name: "MyIngress.mixed" alias: "mixed" }
  match_fields { id: 1 name: "h.key.value" bitwidth: 32 match_type: EXACT }
  match_fields { id: 2 name: "h.key.prefix" bitwidth: 32 match_type: LPM }
  match_fields { id: 3 name: "h.key.flags" bitwidth: 16 match_type: TERNARY }
  match_fields { id: 4 name: "h.key.range" bitwidth: 16 match_type: RANGE }
  match_fields { id: 5 name: "h.key.class" bitwidth: 8 match_type: OPTIONAL }
  action_refs { id: 16777218 } size: 32768
}
actions {
  preamble { id: 16777217 name: "MyIngress.forward" alias: "forward" }
  params { id: 1 name: "port" bitwidth: 9 }
}
actions {
  preamble { id: 16777218 name: "MyIngress.tagged" alias: "tagged" }
  params { id: 1 name: "port" bitwidth: 9 }
  params { id: 2 name: "tag" bitwidth: 32 }
}
''')
    corpus = {}
    for workload in ("exact", "mixed"):
        items = []
        for index in range(256):
            mixed = workload == "mixed"
            matches = [dict(field="h.key.value", id=1, kind="exact", value=integer(0x12340000 + index))]
            params = [dict(name="port", id=1, value=integer(1 + index % 255))]
            if mixed:
                matches += [dict(field="h.key.prefix", id=2, kind="lpm", value=integer(0x0A000000 + (index << 16)), prefix=16),
                            dict(field="h.key.flags", id=3, kind="ternary", value=integer(0x1200 + (index % 16) * 16), mask="fff0"),
                            dict(field="h.key.range", id=4, kind="range", value=integer(100 + index), high=integer(110 + index)),
                            dict(field="h.key.class", id=5, kind="optional", value=integer(index))]
                params += [dict(name="tag", id=2, value=integer(0x10203000 + index))]
            items.append(dict(table="MyIngress.mixed" if mixed else "MyIngress.forwarding",
                              table_id=33554434 if mixed else 33554433,
                              action="MyIngress.tagged" if mixed else "MyIngress.forward",
                              action_id=16777218 if mixed else 16777217,
                              priority=10 if mixed else 0, matches=matches, params=params, metadata="4532"))
        corpus[workload] = items
    (folder / "inputs.json").write_text(json.dumps(corpus, indent=2) + "\n")


if __name__ == "__main__":
    prepare()
