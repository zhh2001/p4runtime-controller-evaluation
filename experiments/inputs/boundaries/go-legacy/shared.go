package main

import (
	"bufio"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	p4i "github.com/p4lang/p4runtime/go/p4/config/v1"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/encoding/prototext"
)

type Case struct {
	ID          string          `json:"id"`
	Token       string          `json:"token"`
	Family      string          `json:"family"`
	Kind        string          `json:"kind"`
	Op          string          `json:"op"`
	Resource    string          `json:"resource"`
	Table       string          `json:"table"`
	Action      string          `json:"action"`
	Field       string          `json:"field"`
	Key         string          `json:"key"`
	ActionValue string          `json:"action_value"`
	Index       int64           `json:"index"`
	Data        json.RawMessage `json:"data"`
}

var info = &p4i.P4Info{}
var rpc p4v1.P4RuntimeClient
var rawMode bool
var notExpressible = errors.New("not expressible")

func unhex(value string) []byte {
	result, err := hex.DecodeString(value)
	if err != nil {
		panic(err)
	}
	return result
}
func unexpressible(reason string) error { return fmt.Errorf("%w: %s", notExpressible, reason) }

func resourceID(kind, name string) uint32 {
	switch kind {
	case "counter":
		for _, v := range info.Counters {
			if v.Preamble.Name == name {
				return v.Preamble.Id
			}
		}
	case "meter":
		for _, v := range info.Meters {
			if v.Preamble.Name == name {
				return v.Preamble.Id
			}
		}
	case "register":
		for _, v := range info.Registers {
			if v.Preamble.Name == name {
				return v.Preamble.Id
			}
		}
	}
	return 0
}

func rawEntry(c Case) *p4v1.Entity {
	var index *p4v1.Index
	if !(c.Op == "read" && c.Index == -1) {
		index = &p4v1.Index{Index: c.Index}
	}
	id := resourceID(c.Kind, c.Resource)
	switch c.Kind {
	case "match", "param":
		var table *p4i.Table
		for _, v := range info.Tables {
			if v.Preamble.Name == c.Table {
				table = v
				break
			}
		}
		var action *p4i.Action
		for _, v := range info.Actions {
			if v.Preamble.Name == c.Action {
				action = v
				break
			}
		}
		entry := &p4v1.TableEntry{TableId: table.Preamble.Id, Match: []*p4v1.FieldMatch{{FieldId: table.MatchFields[0].Id, FieldMatchType: &p4v1.FieldMatch_Exact_{Exact: &p4v1.FieldMatch_Exact{Value: unhex(c.Key)}}}},
			Action: &p4v1.TableAction{Type: &p4v1.TableAction_Action{Action: &p4v1.Action{ActionId: action.Preamble.Id, Params: []*p4v1.Action_Param{{ParamId: action.Params[0].Id, Value: unhex(c.ActionValue)}}}}}}
		return &p4v1.Entity{Entity: &p4v1.Entity_TableEntry{TableEntry: entry}}
	case "counter":
		entry := &p4v1.CounterEntry{CounterId: id, Index: index}
		if c.Op == "write" {
			entry.Data = &p4v1.CounterData{PacketCount: 11, ByteCount: 704}
		}
		return &p4v1.Entity{Entity: &p4v1.Entity_CounterEntry{CounterEntry: entry}}
	case "meter":
		entry := &p4v1.MeterEntry{MeterId: id, Index: index}
		if c.Op == "write" {
			entry.Config = &p4v1.MeterConfig{Cir: 10, Pir: 20, Cburst: 2, Pburst: 4}
		}
		return &p4v1.Entity{Entity: &p4v1.Entity_MeterEntry{MeterEntry: entry}}
	case "register":
		entry := &p4v1.RegisterEntry{RegisterId: id, Index: index}
		if c.Op == "write" {
			entry.Data = &p4v1.P4Data{}
			if err := protojson.Unmarshal(c.Data, entry.Data); err != nil {
				panic(err)
			}
		}
		return &p4v1.Entity{Entity: &p4v1.Entity_RegisterEntry{RegisterEntry: entry}}
	}
	panic(c.Kind)
}

func rawRead(ctx context.Context, entry *p4v1.Entity) ([]*p4v1.Entity, error) {
	stream, err := rpc.Read(ctx, &p4v1.ReadRequest{DeviceId: 1, Entities: []*p4v1.Entity{entry}})
	if err != nil {
		return nil, err
	}
	var out []*p4v1.Entity
	for {
		response, err := stream.Recv()
		if err == io.EOF {
			return out, nil
		}
		if err != nil {
			return nil, err
		}
		out = append(out, response.Entities...)
	}
}

func rawWrite(ctx context.Context, entry *p4v1.Entity) error {
	_, err := rpc.Write(ctx, &p4v1.WriteRequest{DeviceId: 1, ElectionId: &p4v1.Uint128{Low: 1}, Updates: []*p4v1.Update{{Type: p4v1.Update_MODIFY, Entity: entry}}})
	return err
}

func main() {
	address := flag.String("address", "", "")
	p4info := flag.String("p4info", "", "")
	profile := flag.String("profile", "", "")
	flag.Parse()
	rawMode = (*profile == "raw-current" || *profile == "raw-legacy")
	data, err := os.ReadFile(*p4info)
	if err != nil {
		panic(err)
	}
	if err = prototext.Unmarshal(data, info); err != nil {
		panic(err)
	}
	conn, err := grpc.Dial(*address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		panic(err)
	}
	defer conn.Close()
	rpc = p4v1.NewP4RuntimeClient(conn)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	if rawMode || needsApplicationArbitration {
		arbCtx, arbCancel := context.WithCancel(context.Background())
		defer arbCancel()
		stream, openErr := rpc.StreamChannel(arbCtx)
		if openErr != nil {
			panic(openErr)
		}
		if err = stream.Send(&p4v1.StreamMessageRequest{Update: &p4v1.StreamMessageRequest_Arbitration{Arbitration: &p4v1.MasterArbitrationUpdate{DeviceId: 1, ElectionId: &p4v1.Uint128{Low: 1}}}}); err != nil {
			panic(err)
		}
		response, readErr := stream.Recv()
		if readErr != nil || response.GetArbitration().GetStatus().GetCode() != 0 {
			panic(fmt.Sprint(response, readErr))
		}
	}
	var closeNative func()
	if !rawMode {
		closeNative, err = nativeSetup(ctx, *address, data)
		if err != nil {
			panic(err)
		}
		defer closeNative()
	}
	cancel()
	encoder := json.NewEncoder(os.Stdout)
	encoder.Encode(map[string]any{"ready": true, "profile": *profile})
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 65536), 1<<20)
	for scanner.Scan() {
		var c Case
		if err = json.Unmarshal(scanner.Bytes(), &c); err != nil {
			panic(err)
		}
		if c.Op == "close" {
			break
		}
		callCtx, callCancel := context.WithTimeout(context.Background(), 5*time.Second)
		callCtx = metadata.AppendToOutgoingContext(callCtx, "x-e1-case", c.Token)
		var public any
		started := time.Now()
		if rawMode {
			entry := rawEntry(c)
			if c.Op == "read" {
				public, err = rawRead(callCtx, entry)
			} else {
				err = rawWrite(callCtx, entry)
			}
		} else {
			public, err = nativeDo(callCtx, c)
		}
		callCancel()
		row := map[string]any{"id": c.ID, "token": c.Token, "stage": "completed", "rpc_code": int(status.Code(err)), "elapsed_ns": time.Since(started).Nanoseconds(), "public": public}
		if err != nil {
			row["error"] = err.Error()
			if errors.Is(err, notExpressible) {
				row["stage"] = "not_expressible"
			}
		}
		encoder.Encode(row)
	}
	if err = scanner.Err(); err != nil {
		panic(err)
	}
}
