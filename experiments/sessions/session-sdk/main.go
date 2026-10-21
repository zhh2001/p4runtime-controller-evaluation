package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/zhh2001/p4runtime-go-controller/v2/client"
	sdkerrors "github.com/zhh2001/p4runtime-go-controller/v2/errors"
	"github.com/zhh2001/p4runtime-go-controller/v2/pipeline"
	"github.com/zhh2001/p4runtime-go-controller/v2/tableentry"
	"google.golang.org/grpc/status"
)

type command struct {
	Op string `json:"op"`
}

func main() {
	address := flag.String("address", "", "P4Runtime target")
	infoPath := flag.String("p4info", "", "P4Info text")
	flag.Parse()
	output := json.NewEncoder(os.Stdout)
	emit := func(row map[string]any) {
		if err := output.Encode(row); err != nil {
			panic(err)
		}
	}
	data, err := os.ReadFile(*infoPath)
	if err != nil {
		emit(map[string]any{"op": "ready", "error": err.Error()})
		return
	}
	p, err := pipeline.LoadText(data, nil)
	if err != nil {
		emit(map[string]any{"op": "ready", "error": err.Error()})
		return
	}
	entry, err := tableentry.NewBuilder(p, "MyIngress.t_exact").Match("s.ingress_port", tableentry.Exact([]byte{1})).Action("MyIngress.forward", tableentry.Param("port", []byte{1})).Metadata([]byte("session-write")).Build()
	if err != nil {
		emit(map[string]any{"op": "ready", "error": err.Error()})
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	c, err := client.Dial(ctx, *address, client.WithInsecure(), client.WithDeviceID(1), client.WithElectionID(client.ElectionID{Low: 1}))
	cancel()
	if err != nil {
		emit(map[string]any{"op": "ready", "error": err.Error()})
		return
	}
	defer c.Close()
	emit(map[string]any{"op": "ready", "is_primary": c.IsPrimary(), "state": c.State().String()})
	input := json.NewDecoder(os.Stdin)
	for {
		var cmd command
		if err = input.Decode(&cmd); err == io.EOF {
			return
		} else if err != nil {
			panic(err)
		}
		row := map[string]any{"op": cmd.Op}
		ctx, cancel = context.WithTimeout(context.Background(), 60*time.Second)
		err = nil
		switch cmd.Op {
		case "state":
		case "await-primary":
			err = c.BecomePrimary(ctx)
		case "restore-final":
			finalEntry, buildErr := tableentry.NewBuilder(p, "MyIngress.t_exact").Match("s.ingress_port", tableentry.Exact([]byte{1})).Action("MyIngress.forward", tableentry.Param("port", []byte{2})).Metadata([]byte("recovered")).Build()
			if buildErr != nil {
				err = buildErr
			} else {
				err = c.WriteTableEntry(ctx, client.UpdateInsert, finalEntry)
			}
		case "modify-final":
			finalEntry, buildErr := tableentry.NewBuilder(p, "MyIngress.t_exact").Match("s.ingress_port", tableentry.Exact([]byte{1})).Action("MyIngress.forward", tableentry.Param("port", []byte{2})).Metadata([]byte("recovered")).Build()
			if buildErr != nil {
				err = buildErr
			} else {
				err = c.WriteTableEntry(ctx, client.UpdateModify, finalEntry)
			}
		case "write":
			err = c.WriteTableEntry(ctx, client.UpdateInsert, entry)
		case "modify":
			err = c.WriteTableEntry(ctx, client.UpdateModify, entry)
		case "get-pipeline":
			var result *pipeline.Pipeline
			result, err = c.GetPipeline(ctx)
			row["pipeline_present"] = result != nil
		case "close":
			err = c.Close()
		default:
			err = fmt.Errorf("unknown command %q", cmd.Op)
		}
		cancel()
		row["is_primary"] = c.IsPrimary()
		row["state"] = c.State().String()
		row["rpc_code"] = int(status.Code(err))
		row["not_primary"] = errors.Is(err, sdkerrors.ErrNotPrimary)
		row["pipeline_missing"] = errors.Is(err, sdkerrors.ErrPipelineNotSet)
		if err != nil {
			row["error"] = err.Error()
		}
		emit(row)
		if cmd.Op == "close" {
			return
		}
	}
}
