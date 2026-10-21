package main

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"sync"
	"time"

	p4i "github.com/p4lang/p4runtime/go/p4/config/v1"
	p4r "github.com/p4lang/p4runtime/go/p4/v1"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/encoding/prototext"
)

type entry struct {
	MAC  string `json:"mac"`
	Port uint32 `json:"port"`
}
type command struct {
	ID      int     `json:"id"`
	Op      string  `json:"op"`
	Mode    string  `json:"mode"`
	Entries []entry `json:"entries"`
	Index   int64   `json:"index"`
	Payload string  `json:"payload"`
	Port    uint32  `json:"port"`
}

var outputMu sync.Mutex
var rpc p4r.P4RuntimeClient
var stream p4r.P4Runtime_StreamChannelClient
var tableID, fieldID, actionID, paramID, counterID uint32

func mono() int64 {
	var t unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_MONOTONIC, &t); err != nil {
		panic(err)
	}
	return t.Nano()
}
func emit(m map[string]any) {
	outputMu.Lock()
	defer outputMu.Unlock()
	m["monotonic_ns"] = mono()
	if err := json.NewEncoder(os.Stdout).Encode(m); err != nil {
		panic(err)
	}
}
func packet(payload []byte, port uint32) {
	if len(payload) >= 14 && payload[12] == 0x88 && payload[13] == 0xb5 {
		emit(map[string]any{"kind": "packet", "payload": hex.EncodeToString(payload), "ingress_port": port})
	}
}
func number(v uint32) []byte {
	if v < 256 {
		return []byte{byte(v)}
	}
	return []byte{byte(v >> 8), byte(v)}
}
func mac(v string) []byte {
	b, err := hex.DecodeString(v)
	if err != nil || len(b) != 6 {
		panic("invalid MAC")
	}
	return b
}
func canonical(b []byte) []byte {
	for len(b) > 1 && b[0] == 0 {
		b = b[1:]
	}
	return b
}
func metadata(port uint32) []*p4r.PacketMetadata {
	return []*p4r.PacketMetadata{{MetadataId: 1, Value: number(port)}, {MetadataId: 2, Value: []byte{0}}}
}
func rawEntry(e entry) *p4r.TableEntry {
	return &p4r.TableEntry{TableId: tableID, Match: []*p4r.FieldMatch{{FieldId: fieldID, FieldMatchType: &p4r.FieldMatch_Exact_{Exact: &p4r.FieldMatch_Exact{Value: canonical(mac(e.MAC))}}}}, Action: &p4r.TableAction{Type: &p4r.TableAction_Action{Action: &p4r.Action{ActionId: actionID, Params: []*p4r.Action_Param{{ParamId: paramID, Value: number(e.Port)}}}}}}
}
func rawOpen(ctx context.Context, address string) (func(), error) {
	conn, err := grpc.DialContext(ctx, address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	rpc = p4r.NewP4RuntimeClient(conn)
	sc, stop := context.WithCancel(context.Background())
	stream, err = rpc.StreamChannel(sc)
	if err != nil {
		stop()
		conn.Close()
		return nil, err
	}
	ready := make(chan error, 1)
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			r, e := stream.Recv()
			if e != nil {
				select {
				case ready <- e:
				default:
				}
				return
			}
			if a := r.GetArbitration(); a != nil {
				if a.GetStatus().GetCode() == 0 {
					select {
					case ready <- nil:
					default:
					}
				}
			}
			if p := r.GetPacket(); p != nil {
				var port uint32
				for _, m := range p.Metadata {
					if m.MetadataId == 1 {
						for _, v := range m.Value {
							port = port*256 + uint32(v)
						}
					}
				}
				packet(p.Payload, port)
			}
		}
	}()
	err = stream.Send(&p4r.StreamMessageRequest{Update: &p4r.StreamMessageRequest_Arbitration{Arbitration: &p4r.MasterArbitrationUpdate{DeviceId: 1, ElectionId: &p4r.Uint128{Low: 1}}}})
	if err == nil {
		select {
		case err = <-ready:
		case <-ctx.Done():
			err = ctx.Err()
		}
	}
	cleanup := func() { stop(); conn.Close(); <-done }
	if err != nil {
		cleanup()
		return nil, err
	}
	return cleanup, nil
}
func rawWrite(ctx context.Context, u []*p4r.Update) error {
	_, err := rpc.Write(ctx, &p4r.WriteRequest{DeviceId: 1, ElectionId: &p4r.Uint128{Low: 1}, Updates: u})
	return err
}
func rawCounter(ctx context.Context, index int64) (int64, int64, error) {
	s, err := rpc.Read(ctx, &p4r.ReadRequest{DeviceId: 1, Entities: []*p4r.Entity{{Entity: &p4r.Entity_CounterEntry{CounterEntry: &p4r.CounterEntry{CounterId: counterID, Index: &p4r.Index{Index: index}}}}}})
	if err != nil {
		return 0, 0, err
	}
	var found []*p4r.CounterEntry
	for {
		r, e := s.Recv()
		if e == io.EOF {
			break
		}
		if e != nil {
			return 0, 0, e
		}
		for _, entity := range r.Entities {
			found = append(found, entity.GetCounterEntry())
		}
	}
	if len(found) != 1 {
		return 0, 0, fmt.Errorf("expected one counter, got %d", len(found))
	}
	d := found[0].GetData()
	return d.GetPacketCount(), d.GetByteCount(), nil
}
func main() {
	address := flag.String("address", "", "target")
	info := flag.String("p4info", "", "schema")
	raw := flag.Bool("raw", false, "direct protobuf/gRPC")
	flag.Parse()
	data, err := os.ReadFile(*info)
	if err != nil {
		panic(err)
	}
	schema := &p4i.P4Info{}
	if err = prototext.Unmarshal(data, schema); err != nil {
		panic(err)
	}
	for _, t := range schema.Tables {
		if t.Preamble.Name == "MyIngress.t_l2" {
			tableID = t.Preamble.Id
			fieldID = t.MatchFields[0].Id
		}
	}
	for _, a := range schema.Actions {
		if a.Preamble.Name == "MyIngress.forward" {
			actionID = a.Preamble.Id
			paramID = a.Params[0].Id
		}
	}
	for _, c := range schema.Counters {
		if c.Preamble.Name == "MyIngress.pkt_counter" {
			counterID = c.Preamble.Id
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	var cleanup func()
	if *raw {
		cleanup, err = rawOpen(ctx, *address)
	} else {
		cleanup, err = nativeOpen(ctx, *address, data)
	}
	cancel()
	if err != nil {
		emit(map[string]any{"kind": "ready", "error": err.Error()})
		os.Exit(1)
	}
	defer cleanup()
	emit(map[string]any{"kind": "ready"})
	decoder := json.NewDecoder(os.Stdin)
	for {
		var c command
		if err = decoder.Decode(&c); err == io.EOF {
			return
		} else if err != nil {
			panic(err)
		}
		if c.Op == "close" {
			emit(map[string]any{"kind": "reply", "id": c.ID, "ok": true})
			return
		}
		ctx, stop := context.WithTimeout(context.Background(), 10*time.Second)
		row := map[string]any{"kind": "reply", "id": c.ID, "op": c.Op}
		err = nil
		switch c.Op {
		case "write":
			updates := []*p4r.Update{}
			kind := p4r.Update_INSERT
			if c.Mode == "modify" {
				kind = p4r.Update_MODIFY
			}
			for _, e := range c.Entries {
				var item *p4r.TableEntry
				if *raw {
					item = rawEntry(e)
				} else {
					item, err = nativeEntry(e)
				}
				if err != nil {
					break
				}
				updates = append(updates, &p4r.Update{Type: kind, Entity: &p4r.Entity{Entity: &p4r.Entity_TableEntry{TableEntry: item}}})
			}
			if err == nil {
				if *raw {
					err = rawWrite(ctx, updates)
				} else {
					err = nativeWrite(ctx, updates)
				}
			}
		case "counter":
			var packets, bytes int64
			if *raw {
				packets, bytes, err = rawCounter(ctx, c.Index)
			} else {
				packets, bytes, err = nativeCounter(ctx, c.Index)
			}
			row["packets"], row["bytes"] = packets, bytes
		case "send":
			var payload []byte
			payload, err = hex.DecodeString(c.Payload)
			if err == nil {
				if *raw {
					err = stream.Send(&p4r.StreamMessageRequest{Update: &p4r.StreamMessageRequest_Packet{Packet: &p4r.PacketOut{Payload: payload, Metadata: metadata(c.Port)}}})
				} else {
					err = nativeSend(ctx, payload, c.Port)
				}
			}
		default:
			err = fmt.Errorf("unknown operation %q", c.Op)
		}
		stop()
		row["ok"] = err == nil
		if err != nil {
			row["error"] = err.Error()
		}
		emit(row)
	}
}
