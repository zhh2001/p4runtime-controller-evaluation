package construction

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

type Bytes []byte

func (v *Bytes) UnmarshalJSON(data []byte) error {
	var text string
	if err := json.Unmarshal(data, &text); err != nil {
		return err
	}
	value, err := hex.DecodeString(text)
	*v = value
	return err
}

type Match struct {
	Field  string `json:"field"`
	ID     uint32 `json:"id"`
	Kind   string `json:"kind"`
	Value  Bytes  `json:"value"`
	Mask   Bytes  `json:"mask"`
	High   Bytes  `json:"high"`
	Prefix int32  `json:"prefix"`
}

type Param struct {
	Name  string `json:"name"`
	ID    uint32 `json:"id"`
	Value Bytes  `json:"value"`
}

type Input struct {
	Table    string  `json:"table"`
	TableID  uint32  `json:"table_id"`
	Action   string  `json:"action"`
	ActionID uint32  `json:"action_id"`
	Priority int32   `json:"priority"`
	Matches  []Match `json:"matches"`
	Params   []Param `json:"params"`
	Metadata Bytes   `json:"metadata"`
}

type Factory func(*Input) *p4v1.TableEntry

func readInputs(t testing.TB) map[string][]Input {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(os.Getenv("CONSTRUCTION_ROOT"), "fixtures/inputs.json"))
	if err != nil {
		t.Fatal(err)
	}
	var inputs map[string][]Input
	if err = json.Unmarshal(data, &inputs); err != nil {
		t.Fatal(err)
	}
	return inputs
}

func copyBytes(value []byte) []byte { return bytes.Clone(value) }

func rawFactory() Factory {
	return func(input *Input) *p4v1.TableEntry {
		entry := &p4v1.TableEntry{TableId: input.TableID, Priority: input.Priority, Metadata: copyBytes(input.Metadata)}
		for _, match := range input.Matches {
			field := &p4v1.FieldMatch{FieldId: match.ID}
			switch match.Kind {
			case "exact":
				field.FieldMatchType = &p4v1.FieldMatch_Exact_{Exact: &p4v1.FieldMatch_Exact{Value: copyBytes(match.Value)}}
			case "lpm":
				field.FieldMatchType = &p4v1.FieldMatch_Lpm{Lpm: &p4v1.FieldMatch_LPM{Value: copyBytes(match.Value), PrefixLen: match.Prefix}}
			case "ternary":
				field.FieldMatchType = &p4v1.FieldMatch_Ternary_{Ternary: &p4v1.FieldMatch_Ternary{Value: copyBytes(match.Value), Mask: copyBytes(match.Mask)}}
			case "range":
				field.FieldMatchType = &p4v1.FieldMatch_Range_{Range: &p4v1.FieldMatch_Range{Low: copyBytes(match.Value), High: copyBytes(match.High)}}
			case "optional":
				field.FieldMatchType = &p4v1.FieldMatch_Optional_{Optional: &p4v1.FieldMatch_Optional{Value: copyBytes(match.Value)}}
			default:
				panic(match.Kind)
			}
			entry.Match = append(entry.Match, field)
		}
		action := &p4v1.Action{ActionId: input.ActionID}
		for _, param := range input.Params {
			action.Params = append(action.Params, &p4v1.Action_Param{ParamId: param.ID, Value: copyBytes(param.Value)})
		}
		entry.Action = &p4v1.TableAction{Type: &p4v1.TableAction_Action{Action: action}}
		return entry
	}
}

func factory(profile, workload, mode string) Factory {
	if profile == rawProfile {
		return rawFactory()
	}
	return nativeFactory(workload, mode)
}

func modes(profile string) []string {
	if profile == rawProfile {
		return []string{"new"}
	}
	return []string{"new", "reused"}
}

func BenchmarkConstruct(b *testing.B) {
	inputs := readInputs(b)
	if err := setupNative(); err != nil {
		b.Fatal(err)
	}
	for _, profile := range []string{nativeProfile, rawProfile} {
		for _, workload := range []string{"exact", "mixed"} {
			for _, mode := range modes(profile) {
				b.Run(profile+"/"+workload+"/"+mode, func(b *testing.B) {
					build := factory(profile, workload, mode)
					corpus := inputs[workload]
					var result *p4v1.TableEntry
					index := 0
					b.ReportAllocs()
					for b.Loop() {
						result = build(&corpus[index&255])
						index++
					}
					if result == nil {
						b.Fatal("no constructed entry")
					}
					reportElapsed(b)
				})
			}
		}
	}
}

func reportElapsed(b *testing.B) { b.ReportMetric(float64(b.Elapsed().Nanoseconds()), "elapsed-ns") }

func mutateInput(input *Input) {
	change := func(value []byte) {
		for i := range value {
			value[i] ^= 0x55
		}
	}
	change(input.Metadata)
	for _, match := range input.Matches {
		change(match.Value)
		change(match.Mask)
		change(match.High)
	}
	for _, param := range input.Params {
		change(param.Value)
	}
}

func mutateMessage(message protoreflect.Message) {
	message.Range(func(field protoreflect.FieldDescriptor, value protoreflect.Value) bool {
		if field.IsList() {
			list := value.List()
			for i := 0; i < list.Len(); i++ {
				if field.Message() != nil {
					mutateMessage(list.Get(i).Message())
				}
			}
		} else if field.Kind() == protoreflect.BytesKind {
			for i := range value.Bytes() {
				value.Bytes()[i] ^= 0x55
			}
		} else if field.Message() != nil {
			mutateMessage(value.Message())
		}
		return true
	})
}

func TestCorpusAndOwnership(t *testing.T) {
	inputs := readInputs(t)
	if err := setupNative(); err != nil {
		t.Fatal(err)
	}
	var samples []map[string]any
	for _, profile := range []string{nativeProfile, rawProfile} {
		for _, workload := range []string{"exact", "mixed"} {
			for _, mode := range modes(profile) {
				t.Run(profile+"/"+workload+"/"+mode, func(t *testing.T) {
					build := factory(profile, workload, mode)
					for index := range inputs[workload] {
						entry := build(&inputs[workload][index])
						data, err := protojson.Marshal(entry)
						if err != nil {
							t.Fatal(err)
						}
						samples = append(samples, map[string]any{"profile": profile, "workload": workload, "mode": mode, "index": index, "wire": json.RawMessage(data)})
					}
					input := &inputs[workload][0]
					first := build(input)
					before := proto.Clone(first)
					mutateInput(input)
					if !proto.Equal(first, before) {
						t.Fatal("result aliases input bytes")
					}
					mutateInput(input)
					second := build(&inputs[workload][1])
					if !proto.Equal(first, before) {
						t.Fatal("next build changes retained result")
					}
					secondBefore := proto.Clone(second)
					inputBefore, _ := json.Marshal(input)
					mutateMessage(first.ProtoReflect())
					inputAfter, _ := json.Marshal(input)
					if !bytes.Equal(inputBefore, inputAfter) || !proto.Equal(second, secondBefore) {
						t.Fatal("output mutation changes input or another result")
					}
				})
			}
		}
	}
	if name := os.Getenv("CONSTRUCTION_SAMPLES"); name != "" {
		data, err := json.Marshal(samples)
		if err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(name, data, 0644); err != nil {
			t.Fatal(err)
		}
	}
}
