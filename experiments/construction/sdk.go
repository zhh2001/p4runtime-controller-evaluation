package construction

import (
	"os"
	"path/filepath"

	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"github.com/zhh2001/p4runtime-go-controller/v2/pipeline"
	"github.com/zhh2001/p4runtime-go-controller/v2/tableentry"
)

const nativeProfile = "sdk"
const rawProfile = "raw-current"

var pipe *pipeline.Pipeline

func setupNative() error {
	data, err := os.ReadFile(filepath.Join(os.Getenv("CONSTRUCTION_ROOT"), "fixtures/construct.p4info.txt"))
	if err != nil {
		return err
	}
	pipe, err = pipeline.LoadText(data, nil)
	return err
}

func nativeFactory(workload, mode string) Factory {
	table := "MyIngress.forwarding"
	if workload == "mixed" {
		table = "MyIngress.mixed"
	}
	var cached *tableentry.Builder
	if mode == "reused" {
		cached = tableentry.NewBuilder(pipe, table)
	}
	return func(input *Input) *p4v1.TableEntry {
		builder := cached
		if builder == nil {
			builder = tableentry.NewBuilder(pipe, input.Table)
		}
		builder.Priority(input.Priority).Metadata(input.Metadata)
		for _, match := range input.Matches {
			switch match.Kind {
			case "exact":
				builder.Match(match.Field, tableentry.Exact(match.Value))
			case "lpm":
				builder.Match(match.Field, tableentry.LPM(match.Value, match.Prefix))
			case "ternary":
				builder.Match(match.Field, tableentry.Ternary(match.Value, match.Mask))
			case "range":
				builder.Match(match.Field, tableentry.Range(match.Value, match.High))
			case "optional":
				builder.Match(match.Field, tableentry.Optional(match.Value))
			default:
				panic(match.Kind)
			}
		}
		params := make([]tableentry.ActionParam, len(input.Params))
		for i, param := range input.Params {
			params[i] = tableentry.Param(param.Name, param.Value)
		}
		entry, err := builder.Action(input.Action, params...).Build()
		if err != nil {
			panic(err)
		}
		return entry
	}
}
