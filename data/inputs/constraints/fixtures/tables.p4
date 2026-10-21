#include <core.p4>
#include <v1model.p4>
header key_t { bit<8> x; bit<8> y; }
struct headers_t { key_t key; }
struct metadata_t { bit<8> tag; }
parser MyParser(packet_in p, out headers_t h, inout metadata_t m, inout standard_metadata_t s) {
    state start { p.extract(h.key); transition accept; }
}
control Verify(inout headers_t h, inout metadata_t m) { apply {} }
control Compute(inout headers_t h, inout metadata_t m) { apply {} }
control MyIngress(inout headers_t h, inout metadata_t m, inout standard_metadata_t s) {
    action common(bit<9> port) { s.egress_spec = port; }
    action table_action(bit<9> port) { s.egress_spec = port; }
    action default_action(bit<9> port) { s.egress_spec = port; }
    action sparse_action(@id(19) bit<9> port, @id(7) bit<8> tag) { s.egress_spec = port; m.tag = tag; }
    table direct {
        key = { h.key.x: exact; }
        actions = { common; NoAction; }
        default_action = common(0); size = 16;
    }
    table scoped {
        key = { h.key.x: exact; }
        actions = { common; @tableonly table_action; @defaultonly default_action; NoAction; }
        default_action = common(0); size = 16;
    }
    table const_default {
        key = { h.key.x: exact; }
        actions = { common; NoAction; }
        const default_action = common(0); size = 16;
    }
    table const_entries {
        key = { h.key.x: exact; }
        actions = { common; NoAction; }
        default_action = common(0); size = 16;
        const entries = { 1: common(1); }
    }
    action_profile(16) profile;
    table indirect {
        key = { h.key.x: exact; }
        actions = { common; }
        implementation = profile; size = 16;
    }
    action_selector(HashAlgorithm.crc32, 16, 8) selector;
    table selected {
        key = { h.key.x: exact; h.key.y: selector; }
        actions = { common; }
        implementation = selector; size = 16;
    }
    table idle {
        key = { h.key.x: exact; }
        actions = { common; NoAction; }
        default_action = common(0); support_timeout = true; size = 16;
    }
    table sparse {
        key = { h.key.x: exact @id(13); h.key.y: exact @id(5); }
        actions = { sparse_action; NoAction; }
        default_action = NoAction(); size = 16;
    }
    table ternary_table {
        key = { h.key.x: ternary; }
        actions = { common; NoAction; }
        default_action = common(0); size = 16;
    }
    apply {
        direct.apply(); scoped.apply(); const_default.apply(); const_entries.apply();
        indirect.apply(); selected.apply(); idle.apply(); sparse.apply(); ternary_table.apply();
    }
}
control MyEgress(inout headers_t h, inout metadata_t m, inout standard_metadata_t s) { apply {} }
control MyDeparser(packet_out p, in headers_t h) { apply { p.emit(h.key); } }
V1Switch(MyParser(), Verify(), MyIngress(), MyEgress(), Compute(), MyDeparser()) main;
