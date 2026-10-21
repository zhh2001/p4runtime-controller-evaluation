"""Generate the fixed E1 extension and its P4Info declarations."""

import base64
import json
from pathlib import Path
import subprocess

from finsy.proto import p4i, p4t
from google.protobuf import text_format


ROOT = Path(__file__).resolve().parent
WIDTHS = [1, 7, 8, 9, 16, 32, 48, 64, 128, 129]


def unsigned(value):
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big").hex()


def signed(value):
    count = 1
    while True:
        try:
            return value.to_bytes(count, "big", signed=True).hex()
        except OverflowError:
            count += 1


def bitstring(value):
    return dict(bitstring=base64.b64encode(bytes.fromhex(value)).decode())


def integer_type(width, signed=False):
    result = p4t.P4DataTypeSpec()
    getattr(result.bitstring, "int" if signed else "bit").bitwidth = width
    return result


def build():
    folder = ROOT / "fixtures"
    folder.mkdir(exist_ok=True)
    members = "\n".join(f"  bit<{w}> w{w};" for w in WIDTHS)
    declarations, applies = [], []
    for width in WIDTHS:
        declarations.append(f'''action a_w{width}(bit<{width}> value) {{ s.egress_spec = (bit<9>)value; }}
    table t_w{width} {{
        key = {{ h.key.w{width}: exact; }}
        actions = {{ a_w{width}; NoAction; }}
        default_action = NoAction(); size = 16;
    }}''')
        applies.append(f"t_w{width}.apply();")
    source = '''#include <core.p4>
#include <v1model.p4>
header key_t {
MEMBERS
    bit<6> padding;
}
struct headers_t { key_t key; }
struct metadata_t { bit<2> color; }
parser MyParser(packet_in p, out headers_t h, inout metadata_t m, inout standard_metadata_t s) {
    state start { p.extract(h.key); transition accept; }
}
control Verify(inout headers_t h, inout metadata_t m) { apply {} }
control Compute(inout headers_t h, inout metadata_t m) { apply {} }
control MyIngress(inout headers_t h, inout metadata_t m, inout standard_metadata_t s) {
    counter(8, CounterType.packets_and_bytes) c_idx;
    meter(8, MeterType.packets) m_idx;
    DECLARATIONS
    apply {
        APPLIES
        c_idx.count((bit<32>)s.ingress_port);
        m_idx.execute_meter((bit<32>)s.ingress_port, m.color);
    }
}
control MyEgress(inout headers_t h, inout metadata_t m, inout standard_metadata_t s) { apply {} }
control MyDeparser(packet_out p, in headers_t h) { apply { p.emit(h.key); } }
V1Switch(MyParser(), Verify(), MyIngress(), MyEgress(), Compute(), MyDeparser()) main;
'''.replace("MEMBERS", members).replace("DECLARATIONS", "\n    ".join(declarations)).replace("APPLIES", " ".join(applies))
    (folder / "boundaries.p4").write_text(source)
    command = ["p4c-bm2-ss", "--p4v", "16", "--p4runtime-files", "compiler.p4info.txt",
               "-o", "boundaries.json", "boundaries.p4"]
    result = subprocess.run(command, cwd=folder, capture_output=True, text=True)
    (folder / "compiler.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr
    info = text_format.Parse((folder / "compiler.p4info.txt").read_text(), p4i.P4Info())
    for table in info.tables:
        table.ClearField("initial_default_action")
    (folder / "target.p4info.txt").write_text(text_format.MessageToString(info))
    assert len(info.tables) == 10 and len(info.counters) == len(info.meters) == 1
    registry = {}

    def register(name, spec, translated=False):
        entry = info.registers.add(size=8, type_spec=spec)
        entry.preamble.id = 0x16000000 + len(info.registers)
        entry.preamble.name = "MyIngress." + name
        entry.preamble.alias = name
        if translated:
            entry.index_type_name.name = "PortIndex"
        registry[name] = entry

    for width in WIDTHS:
        register(f"r_u{width}", integer_type(width))
        register(f"r_s{width}", integer_type(width, True))
    pair = p4t.P4DataTypeSpec(tuple=p4t.P4TupleTypeSpec(members=[integer_type(9), p4t.P4DataTypeSpec(bool=p4t.P4BoolType())]))
    register("r_tuple", pair)
    member = info.type_info.structs["Pair"]
    member.members.add(name="count", type_spec=integer_type(9))
    member.members.add(name="flag", type_spec=p4t.P4DataTypeSpec(bool=p4t.P4BoolType()))
    struct = p4t.P4DataTypeSpec(struct=p4t.P4NamedType(name="Pair"))
    register("r_struct", struct)
    info.type_info.new_types["Alias"].original_type.CopyFrom(integer_type(9))
    info.type_info.new_types["SdnBits"].translated_type.CopyFrom(p4t.P4NewTypeTranslation(uri="example.org/e1/bits", sdn_bitwidth=16))
    info.type_info.new_types["SdnText"].translated_type.CopyFrom(p4t.P4NewTypeTranslation(uri="example.org/e1/text", sdn_string=p4t.P4NewTypeTranslation.SdnString()))
    info.type_info.new_types["PortIndex"].translated_type.CopyFrom(p4t.P4NewTypeTranslation(uri="example.org/e1/index", sdn_bitwidth=32))
    for name, type_name in (("r_alias", "Alias"), ("r_translated", "SdnBits"), ("r_text", "SdnText")):
        register(name, p4t.P4DataTypeSpec(new_type=p4t.P4NamedType(name=type_name)))
    register("r_varbit", p4t.P4DataTypeSpec(bitstring=p4t.P4BitstringLikeTypeSpec(varbit=p4t.P4VarbitTypeSpec(max_bitwidth=32))))
    register("r_nested", p4t.P4DataTypeSpec(tuple=p4t.P4TupleTypeSpec(members=[integer_type(128), struct])))
    register("r_idx", integer_type(9), True)
    for kind, field, spec in (("counter", "counters", p4i.CounterSpec(unit=p4i.CounterSpec.BOTH)),
                              ("meter", "meters", p4i.MeterSpec(unit=p4i.MeterSpec.PACKETS))):
        item = getattr(info, field).add(size=8, spec=spec)
        item.preamble.name = "MyIngress." + ("c_translated" if kind == "counter" else "m_translated")
        item.preamble.alias = item.preamble.name.split(".")[-1]
        item.preamble.id = (0x12000000 if kind == "counter" else 0x14000000) + 1
        assert len({e.preamble.id for e in getattr(info, field)}) == 2
        item.index_type_name.name = "PortIndex"
    (folder / "controlled.p4info.txt").write_text(text_format.MessageToString(info))
    cases = []

    def add(case_id, family, category="valid", **fields):
        cases.append(dict(id=case_id, family=family, category=category, op="write", **fields))

    for width in WIDTHS:
        for label, value, category, encoded in (("zero", 0, "valid", "00"),
                                                ("max", (1 << width) - 1, "valid", unsigned((1 << width) - 1)),
                                                ("overflow", 1 << width, "invalid", unsigned(1 << width)),
                                                ("padded", 1, "normalizable", "000001")):
            for kind in ("match", "param"):
                add(f"{kind}-{width}-{label}", "wide_" + kind, category, kind=kind, width=width,
                    table=f"MyIngress.t_w{width}", action=f"MyIngress.a_w{width}", field=f"h.key.w{width}",
                    value=encoded, key=encoded if kind == "match" else "01", action_value=encoded if kind == "param" else "01",
                    expected=dict(key=value if kind == "match" else 1, value=value if kind == "param" else 1), native=value)
            add(f"uint-{width}-{label}", "register_unsigned", category, kind="register", resource=f"MyIngress.r_u{width}",
                index=0, data=bitstring(encoded), native=value, expected=value, width=width,
                native_text="0x" + encoded if label == "padded" else "")
        for label, value in (("zero", 0), ("min", -(1 << (width-1))), ("max", (1 << (width-1))-1),
                             ("underflow", -(1 << (width-1))-1), ("overflow", 1 << (width-1)),
                             ("sign-extended", -(1 << (width-1)))):
            encoded = signed(value)
            if label == "sign-extended":
                encoded = "ffff" + encoded
            add(f"signed-{width}-{label}", "register_signed", "invalid" if label in ("underflow", "overflow") else "normalizable" if label == "sign-extended" else "valid",
                kind="register", resource=f"MyIngress.r_s{width}", index=0, data=bitstring(encoded), native=value,
                expected=value, width=width, native_unexpressible=["finsy"] if label == "sign-extended" else [])
    for kind in ("counter", "meter", "register"):
        for translated in (False, True):
            resource = {"counter": "c_translated" if translated else "c_idx",
                        "meter": "m_translated" if translated else "m_idx",
                        "register": "r_idx" if translated else "r_u9"}[kind]
            indexes = [-2, -1, 0, 7, 8, 10000, 10001, 10002] if translated else [-2, -1, 0, 7, 8, (1 << 63)-1]
            for op in ("read", "write"):
                for index in indexes:
                    valid = op == "read" and index == -1 or index in ([10000, 10001] if translated else [0, 7])
                    case = dict(id=f"index-{kind}-{'translated' if translated else 'array'}-{op}-{index}", family="resource_index",
                                category="valid" if valid else "invalid", kind=kind, op=op, index=index,
                                resource="MyIngress." + resource, translated=translated,
                                data=bitstring("01") if kind == "register" else None, native=1, expected=1)
                    cases.append(case)

    def typed(name, label, data, native, expected, category="valid"):
        add(f"typed-{name}-{label}", "register_compound", category, kind="register", resource="MyIngress.r_"+name,
            index=0, data=data, native=native, expected=expected)

    for name, tag in (("tuple", "tuple"), ("struct", "struct")):
        for label, values in (("valid", [5, True]), ("missing", [5]), ("extra", [5, True, False]), ("overflow", [512, True])):
            members = [bitstring(unsigned(values[0]))] + [dict(bool=v) for v in values[1:]]
            native = values if name == "tuple" else dict(zip(["count", "flag", "extra"], values))
            typed(name, label, {tag: dict(members=members)}, native, values,
                  "valid" if label == "valid" else "invalid")
    for label, value, encoded in (("valid", 5, "05"), ("padded", 5, "0005"), ("overflow", 512, "0200")):
        typed("alias", label, bitstring(encoded), value, value, "invalid" if label == "overflow" else "normalizable" if label == "padded" else "valid")
    for label, value in (("max", 65535), ("overflow", 65536)):
        typed("translated", label, bitstring(unsigned(value)), value, value, "valid" if label == "max" else "invalid")
    for label, value in (("utf8", "port-α"), ("empty", "")):
        typed("text", label, bitstring(value.encode().hex()), value, dict(text_bytes=value.encode().hex()))
    for label, value, width in (("nine", 257, 9), ("sixteen", 32769, 16), ("overflow", 513, 9),
                               ("over-max", 1, 33), ("negative-width", 1, -1)):
        encoded = unsigned(value)
        if width > 0:
            encoded = encoded.zfill(((width+7)//8)*2)
        typed("varbit", label, dict(varbit=dict(bitstring=base64.b64encode(bytes.fromhex(encoded)).decode(), bitwidth=width)),
              [value, width], dict(value=value, bitwidth=width), "valid" if label in ("nine", "sixteen") else "invalid")
    for label, value in (("valid", 7), ("overflow", 512)):
        data = dict(tuple=dict(members=[bitstring(unsigned(1 << 127)), dict(struct=dict(members=[bitstring(unsigned(value)), dict(bool=True)]))]))
        typed("nested", label, data, [1 << 127, dict(count=value, flag=True)], [1 << 127, [value, True]], "valid" if label == "valid" else "invalid")
    assert len(cases) == 286 and len({c["id"] for c in cases}) == len(cases)
    (folder / "cases.json").write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(dict(cases=len(cases), widths=WIDTHS, registers=len(info.registers))))


if __name__ == "__main__":
    build()
