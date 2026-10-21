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
	"google.golang.org/protobuf/encoding/prototext"
)

type NamedValue struct {
	Name  string `json:"name"`
	Value string `json:"value"`
	Kind  string `json:"kind"`
	Mask  string `json:"mask"`
}
type Case struct {
	ID         string       `json:"id"`
	Token      string       `json:"token"`
	Family     string       `json:"family"`
	Op         string       `json:"op"`
	Table      string       `json:"table"`
	Action     string       `json:"action"`
	ActionKind string       `json:"action_kind"`
	Reference  uint32       `json:"reference"`
	Update     string       `json:"update"`
	Default    bool         `json:"default"`
	Priority   int32        `json:"priority"`
	TTL        int64        `json:"ttl"`
	Matches    []NamedValue `json:"matches"`
	Params     []NamedValue `json:"params"`
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

func rawEntry(c Case) *p4v1.Entity {
	var table *p4i.Table
	for _, v := range info.Tables {
		if v.Preamble.Name == c.Table {
			table = v
			break
		}
	}
	entry := &p4v1.TableEntry{TableId: table.Preamble.Id, IsDefaultAction: c.Default, Priority: c.Priority, IdleTimeoutNs: c.TTL}
	for _, v := range c.Matches {
		id := uint32(0xffffff)
		for _, f := range table.MatchFields {
			if f.Name == v.Name {
				id = f.Id
			}
		}
		mf := &p4v1.FieldMatch{FieldId: id}
		if v.Kind == "exact" {
			mf.FieldMatchType = &p4v1.FieldMatch_Exact_{Exact: &p4v1.FieldMatch_Exact{Value: unhex(v.Value)}}
		} else {
			mf.FieldMatchType = &p4v1.FieldMatch_Ternary_{Ternary: &p4v1.FieldMatch_Ternary{Value: unhex(v.Value), Mask: unhex(v.Mask)}}
		}
		entry.Match = append(entry.Match, mf)
	}
	switch c.ActionKind {
	case "direct":
		var act *p4i.Action
		for _, v := range info.Actions {
			if v.Preamble.Name == c.Action {
				act = v
				break
			}
		}
		action := &p4v1.Action{ActionId: act.Preamble.Id}
		for _, v := range c.Params {
			id := uint32(0xffffff)
			for _, p := range act.Params {
				if p.Name == v.Name {
					id = p.Id
				}
			}
			action.Params = append(action.Params, &p4v1.Action_Param{ParamId: id, Value: unhex(v.Value)})
		}
		entry.Action = &p4v1.TableAction{Type: &p4v1.TableAction_Action{Action: action}}
	case "member":
		entry.Action = &p4v1.TableAction{Type: &p4v1.TableAction_ActionProfileMemberId{ActionProfileMemberId: c.Reference}}
	case "group":
		entry.Action = &p4v1.TableAction{Type: &p4v1.TableAction_ActionProfileGroupId{ActionProfileGroupId: c.Reference}}
	}
	return &p4v1.Entity{Entity: &p4v1.Entity_TableEntry{TableEntry: entry}}
}

func updateType(c Case) p4v1.Update_Type { return p4v1.Update_Type(p4v1.Update_Type_value[c.Update]) }

func nativeSafe(ctx context.Context, c Case) (public any, err error) {
	defer func() {
		if value := recover(); value != nil {
			err = fmt.Errorf("adapter recovered panic: %v", value)
		}
	}()
	return nativeDo(ctx, c)
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

func rawWrite(ctx context.Context, c Case, entry *p4v1.Entity) error {
	_, err := rpc.Write(ctx, &p4v1.WriteRequest{DeviceId: 1, ElectionId: &p4v1.Uint128{Low: 1}, Updates: []*p4v1.Update{{Type: updateType(c), Entity: entry}}})
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
				err = rawWrite(callCtx, c, entry)
			}
		} else {
			public, err = nativeSafe(callCtx, c)
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
