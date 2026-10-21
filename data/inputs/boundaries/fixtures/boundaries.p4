#include <core.p4>
#include <v1model.p4>
header key_t {
  bit<1> w1;
  bit<7> w7;
  bit<8> w8;
  bit<9> w9;
  bit<16> w16;
  bit<32> w32;
  bit<48> w48;
  bit<64> w64;
  bit<128> w128;
  bit<129> w129;
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
    action a_w1(bit<1> value) { s.egress_spec = (bit<9>)value; }
    table t_w1 {
        key = { h.key.w1: exact; }
        actions = { a_w1; NoAction; }
        default_action = NoAction(); size = 16;
    }
    action a_w7(bit<7> value) { s.egress_spec = (bit<9>)value; }
    table t_w7 {
        key = { h.key.w7: exact; }
        actions = { a_w7; NoAction; }
        default_action = NoAction(); size = 16;
    }
    action a_w8(bit<8> value) { s.egress_spec = (bit<9>)value; }
    table t_w8 {
        key = { h.key.w8: exact; }
        actions = { a_w8; NoAction; }
        default_action = NoAction(); size = 16;
    }
    action a_w9(bit<9> value) { s.egress_spec = (bit<9>)value; }
    table t_w9 {
        key = { h.key.w9: exact; }
        actions = { a_w9; NoAction; }
        default_action = NoAction(); size = 16;
    }
    action a_w16(bit<16> value) { s.egress_spec = (bit<9>)value; }
    table t_w16 {
        key = { h.key.w16: exact; }
        actions = { a_w16; NoAction; }
        default_action = NoAction(); size = 16;
    }
    action a_w32(bit<32> value) { s.egress_spec = (bit<9>)value; }
    table t_w32 {
        key = { h.key.w32: exact; }
        actions = { a_w32; NoAction; }
        default_action = NoAction(); size = 16;
    }
    action a_w48(bit<48> value) { s.egress_spec = (bit<9>)value; }
    table t_w48 {
        key = { h.key.w48: exact; }
        actions = { a_w48; NoAction; }
        default_action = NoAction(); size = 16;
    }
    action a_w64(bit<64> value) { s.egress_spec = (bit<9>)value; }
    table t_w64 {
        key = { h.key.w64: exact; }
        actions = { a_w64; NoAction; }
        default_action = NoAction(); size = 16;
    }
    action a_w128(bit<128> value) { s.egress_spec = (bit<9>)value; }
    table t_w128 {
        key = { h.key.w128: exact; }
        actions = { a_w128; NoAction; }
        default_action = NoAction(); size = 16;
    }
    action a_w129(bit<129> value) { s.egress_spec = (bit<9>)value; }
    table t_w129 {
        key = { h.key.w129: exact; }
        actions = { a_w129; NoAction; }
        default_action = NoAction(); size = 16;
    }
    apply {
        t_w1.apply(); t_w7.apply(); t_w8.apply(); t_w9.apply(); t_w16.apply(); t_w32.apply(); t_w48.apply(); t_w64.apply(); t_w128.apply(); t_w129.apply();
        c_idx.count((bit<32>)s.ingress_port);
        m_idx.execute_meter((bit<32>)s.ingress_port, m.color);
    }
}
control MyEgress(inout headers_t h, inout metadata_t m, inout standard_metadata_t s) { apply {} }
control MyDeparser(packet_out p, in headers_t h) { apply { p.emit(h.key); } }
V1Switch(MyParser(), Verify(), MyIngress(), MyEgress(), Compute(), MyDeparser()) main;
