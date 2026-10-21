package main

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	p4i "github.com/p4lang/p4runtime/go/p4/config/v1"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/encoding/prototext"
)

type Match struct {
	Field  string `json:"field"`
	Kind   string `json:"kind"`
	Value  string `json:"value"`
	Mask   string `json:"mask"`
	High   string `json:"high"`
	Prefix int32  `json:"prefix"`
}
type Case struct {
	ID       string            `json:"id"`
	Suite    string            `json:"suite"`
	Table    string            `json:"table"`
	Matches  []Match           `json:"matches"`
	Action   string            `json:"action"`
	Params   map[string]string `json:"params"`
	Priority int32             `json:"priority"`
	Count    int               `json:"count"`
}
type Item struct {
	Index int   `json:"index"`
	Code  int32 `json:"code"`
}

var info = &p4i.P4Info{}
var rpc p4v1.P4RuntimeClient

func unhex(value string) []byte {
	result, err := hex.DecodeString(value)
	if err != nil {
		panic(err)
	}
	return result
}

func buildRaw(c Case) *p4v1.TableEntry {
	entry := &p4v1.TableEntry{Priority: c.Priority}
	var table *p4i.Table
	for _, candidate := range info.Tables {
		if candidate.Preamble.Name == c.Table {
			table = candidate
			entry.TableId = candidate.Preamble.Id
		}
	}
	for _, m := range c.Matches {
		fm := &p4v1.FieldMatch{}
		if table != nil {
			for _, field := range table.MatchFields {
				if field.Name == m.Field {
					fm.FieldId = field.Id
				}
			}
		}
		switch m.Kind {
		case "exact":
			fm.FieldMatchType = &p4v1.FieldMatch_Exact_{Exact: &p4v1.FieldMatch_Exact{Value: unhex(m.Value)}}
		case "lpm":
			fm.FieldMatchType = &p4v1.FieldMatch_Lpm{Lpm: &p4v1.FieldMatch_LPM{Value: unhex(m.Value), PrefixLen: m.Prefix}}
		case "ternary":
			fm.FieldMatchType = &p4v1.FieldMatch_Ternary_{Ternary: &p4v1.FieldMatch_Ternary{Value: unhex(m.Value), Mask: unhex(m.Mask)}}
		case "range":
			fm.FieldMatchType = &p4v1.FieldMatch_Range_{Range: &p4v1.FieldMatch_Range{Low: unhex(m.Value), High: unhex(m.High)}}
		case "optional":
			fm.FieldMatchType = &p4v1.FieldMatch_Optional_{Optional: &p4v1.FieldMatch_Optional{Value: unhex(m.Value)}}
		}
		entry.Match = append(entry.Match, fm)
	}
	action := &p4v1.Action{}
	var definition *p4i.Action
	for _, candidate := range info.Actions {
		if candidate.Preamble.Name == c.Action {
			action.ActionId = candidate.Preamble.Id
			definition = candidate
		}
	}
	for name, value := range c.Params {
		param := &p4v1.Action_Param{Value: unhex(value)}
		if definition != nil {
			for _, candidate := range definition.Params {
				if candidate.Name == name {
					param.ParamId = candidate.Id
				}
			}
		}
		action.Params = append(action.Params, param)
	}
	entry.Action = &p4v1.TableAction{Type: &p4v1.TableAction_Action{Action: action}}
	return entry
}

func main() {
	output := map[string]any{"stage": "setup_error", "exposed_results": nil, "raw_details": nil}
	defer func() {
		if value := recover(); value != nil {
			output["stage"] = "panic"
			output["error"] = fmt.Sprint(value)
		}
		if err := json.NewEncoder(os.Stdout).Encode(output); err != nil {
			panic(err)
		}
	}()
	address := flag.String("address", "", "controlled endpoint")
	p4info := flag.String("p4info", "", "P4Info file")
	adapter := flag.String("adapter", "native", "native or raw")
	flag.Parse()
	var c Case
	if err := json.NewDecoder(os.Stdin).Decode(&c); err != nil {
		output["error"] = err.Error()
		return
	}
	output["case_id"] = c.ID
	data, err := os.ReadFile(*p4info)
	if err != nil {
		output["error"] = err.Error()
		return
	}
	if err = prototext.Unmarshal(data, info); err != nil {
		output["error"] = err.Error()
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, err := grpc.DialContext(ctx, *address, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithBlock())
	if err != nil {
		output["error"] = err.Error()
		return
	}
	defer conn.Close()
	rpc = p4v1.NewP4RuntimeClient(conn)
	if *adapter == "native" {
		if err = nativeSetup(ctx, *address); err != nil {
			output["error"] = err.Error()
			return
		}
	}
	output["stage"] = "local_rejected"
	marker := []byte(c.Suite + "|" + c.ID + "|" + os.Getenv("P4_ADAPTER"))
	var updates []*p4v1.Update
	if c.Suite == "E1" {
		var entry *p4v1.TableEntry
		if *adapter == "raw" {
			entry = buildRaw(c)
		} else {
			entry, err = nativeBuild(c)
		}
		if err != nil {
			output["error"] = err.Error()
			if strings.HasPrefix(err.Error(), "UNSUPPORTED:") {
				output["stage"] = "unsupported_case"
			}
			return
		}
		entry.Metadata = marker
		wire, _ := protojson.MarshalOptions{UseProtoNames: true}.Marshal(entry)
		output["wire"] = json.RawMessage(wire)
		updates = append(updates, &p4v1.Update{Type: p4v1.Update_INSERT, Entity: &p4v1.Entity{Entity: &p4v1.Entity_TableEntry{TableEntry: entry}}})
	} else {
		for i := 0; i < c.Count; i++ {
			valid := Case{Table: "MyIngress.t_exact", Matches: []Match{{Field: "s.ingress_port", Kind: "exact", Value: fmt.Sprintf("%02x", i+1)}}, Action: "MyIngress.forward", Params: map[string]string{"port": "01"}}
			entry := buildRaw(valid)
			entry.Metadata = marker
			updates = append(updates, &p4v1.Update{Type: p4v1.Update_INSERT, Entity: &p4v1.Entity{Entity: &p4v1.Entity_TableEntry{TableEntry: entry}}})
		}
	}
	if *adapter == "raw" {
		_, err = rpc.Write(ctx, &p4v1.WriteRequest{DeviceId: 1, ElectionId: &p4v1.Uint128{Low: 1}, Updates: updates})
	} else {
		var exposed []Item
		var matches bool
		err, exposed, matches = nativeWrite(ctx, *address, updates)
		output["exposed_results"] = exposed
		output["matches_entry_exists"] = matches
	}
	output["stage"] = "request_completed"
	output["rpc_code"] = int(status.Code(err))
	output["call_succeeded"] = err == nil
	if err != nil {
		output["error"] = err.Error()
		details := []Item{}
		for i, detail := range status.Convert(err).Proto().Details {
			item := &p4v1.Error{}
			if detail.UnmarshalTo(item) != nil {
				output["invalid_raw_detail"] = true
				continue
			}
			details = append(details, Item{Index: i, Code: item.CanonicalCode})
		}
		output["raw_details"] = details
	}
}
