#include <core.p4>
#include <v1model.p4>

header ethernet_t {
    bit<48> dst;
    bit<48> src;
    bit<16> ether_type;
}

header key_t {
    bit<32> value;
}

struct headers_t {
    ethernet_t ethernet;
    key_t key;
}

struct metadata_t {}

parser MyParser(packet_in p, out headers_t h, inout metadata_t m,
                inout standard_metadata_t s) {
    state start {
        p.extract(h.ethernet);
        transition select(h.ethernet.ether_type) {
            0x88b5: read_key;
            default: accept;
        }
    }
    state read_key {
        p.extract(h.key);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers_t h, inout metadata_t m) {
    apply {}
}

control MyComputeChecksum(inout headers_t h, inout metadata_t m) {
    apply {}
}

control MyIngress(inout headers_t h,
                  inout metadata_t m,
                  inout standard_metadata_t s) {
    action forward(bit<9> port) {
        s.egress_spec = port;
    }
    table forwarding {
        key = {
            h.key.value: exact;
        }
        actions = {
            forward;
            NoAction;
        }
        default_action = NoAction();
        size = 32768;
    }
    apply {
        if (h.key.isValid()) {
            forwarding.apply();
        }
    }
}
control MyEgress(inout headers_t h,
                 inout metadata_t m,
                 inout standard_metadata_t s) {
    apply {}
}

control MyDeparser(packet_out p, in headers_t h) {
    apply {
        p.emit(h.ethernet);
        p.emit(h.key);
    }
}

V1Switch(MyParser(),
         MyVerifyChecksum(),
         MyIngress(),
         MyEgress(),
         MyComputeChecksum(),
         MyDeparser()) main;
