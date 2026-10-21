package construction

import (
	"context"
	"os"
	"path/filepath"

	legacy "github.com/antoninbas/p4runtime-go-client/pkg/client"
	p4config "github.com/p4lang/p4runtime/go/p4/config/v1"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/encoding/prototext"
)

const nativeProfile = "legacy"
const rawProfile = "raw-legacy"

var old *legacy.Client

type localConfig struct {
	p4v1.P4RuntimeClient
	info *p4config.P4Info
}

func (c localConfig) GetForwardingPipelineConfig(context.Context, *p4v1.GetForwardingPipelineConfigRequest, ...grpc.CallOption) (*p4v1.GetForwardingPipelineConfigResponse, error) {
	return &p4v1.GetForwardingPipelineConfigResponse{Config: &p4v1.ForwardingPipelineConfig{P4Info: c.info}}, nil
}

func setupNative() error {
	data, err := os.ReadFile(filepath.Join(os.Getenv("CONSTRUCTION_ROOT"), "fixtures/construct.p4info.txt"))
	if err != nil {
		return err
	}
	info := &p4config.P4Info{}
	if err = prototext.Unmarshal(data, info); err != nil {
		return err
	}
	old = legacy.NewClient(localConfig{info: info}, 1, &p4v1.Uint128{Low: 1})
	_, err = old.GetFwdPipe(context.Background(), legacy.GetFwdPipeP4InfoAndCookie)
	return err
}

func newMatches(workload string) map[string]legacy.MatchInterface {
	result := map[string]legacy.MatchInterface{"h.key.value": &legacy.ExactMatch{}}
	if workload == "mixed" {
		result["h.key.prefix"] = &legacy.LpmMatch{}
		result["h.key.flags"] = &legacy.TernaryMatch{}
		result["h.key.range"] = &legacy.RangeMatch{}
		result["h.key.class"] = &legacy.OptionalMatch{}
	}
	return result
}

func nativeFactory(workload, mode string) Factory {
	var cached map[string]legacy.MatchInterface
	if mode == "reused" {
		cached = newMatches(workload)
	}
	return func(input *Input) *p4v1.TableEntry {
		matches := cached
		if matches == nil {
			matches = newMatches(workload)
		}
		for _, match := range input.Matches {
			value := copyBytes(match.Value)
			switch item := matches[match.Field].(type) {
			case *legacy.ExactMatch:
				item.Value = value
			case *legacy.LpmMatch:
				item.Value, item.PLen = value, match.Prefix
			case *legacy.TernaryMatch:
				item.Value, item.Mask = value, copyBytes(match.Mask)
			case *legacy.RangeMatch:
				item.Low, item.High = value, copyBytes(match.High)
			case *legacy.OptionalMatch:
				item.Value = value
			}
		}
		params := make([][]byte, len(input.Params))
		for i, param := range input.Params {
			params[i] = copyBytes(param.Value)
		}
		action := old.NewTableActionDirect(input.Action, params)
		entry := old.NewTableEntry(input.Table, matches, action, &legacy.TableEntryOptions{Priority: input.Priority})
		entry.Metadata = copyBytes(input.Metadata)
		return entry
	}
}
