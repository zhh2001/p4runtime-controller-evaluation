package main

import (
	"context"

	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"github.com/zhh2001/p4runtime-go-controller/v2/client"
	"github.com/zhh2001/p4runtime-go-controller/v2/counter"
	"github.com/zhh2001/p4runtime-go-controller/v2/meter"
	"github.com/zhh2001/p4runtime-go-controller/v2/pipeline"
	"github.com/zhh2001/p4runtime-go-controller/v2/register"
	"github.com/zhh2001/p4runtime-go-controller/v2/tableentry"
	"google.golang.org/protobuf/encoding/protojson"
)

const needsApplicationArbitration = false

var sdk *client.Client
var pipe *pipeline.Pipeline
var counters *counter.Reader
var meters *meter.Reader
var registers *register.Reader

func nativeSetup(ctx context.Context, address string, data []byte) (func(), error) {
	var err error
	pipe, err = pipeline.LoadText(data, nil)
	if err != nil {
		return nil, err
	}
	sdk, err = client.Dial(ctx, address, client.WithInsecure(), client.WithDeviceID(1), client.WithElectionID(client.ElectionID{Low: 1}))
	if err != nil {
		return nil, err
	}
	if err = sdk.BecomePrimary(ctx); err != nil {
		sdk.Close()
		return nil, err
	}
	counters, err = counter.NewReader(sdk, pipe)
	if err != nil {
		return nil, err
	}
	meters, err = meter.NewReader(sdk, pipe)
	if err != nil {
		return nil, err
	}
	registers, err = register.NewReader(sdk, pipe)
	if err != nil {
		return nil, err
	}
	return func() { _ = sdk.Close() }, nil
}

func nativeDo(ctx context.Context, c Case) (any, error) {
	switch c.Kind {
	case "match", "param":
		entry, err := tableentry.NewBuilder(pipe, c.Table).Match(c.Field, tableentry.Exact(unhex(c.Key))).Action(c.Action, tableentry.Param("value", unhex(c.ActionValue))).Build()
		if err != nil {
			return nil, err
		}
		return nil, sdk.Write(ctx, client.WriteOptions{}, &p4v1.Update{Type: p4v1.Update_MODIFY, Entity: &p4v1.Entity{Entity: &p4v1.Entity_TableEntry{TableEntry: entry}}})
	case "counter":
		if c.Op == "read" {
			return counters.Read(ctx, c.Resource, c.Index)
		}
		return nil, counters.Write(ctx, c.Resource, c.Index, 11, 704)
	case "meter":
		if c.Op == "read" {
			return meters.Read(ctx, c.Resource, c.Index)
		}
		return nil, meters.Write(ctx, c.Resource, c.Index, meter.Config{CIR: 10, PIR: 20, CBurst: 2, PBurst: 4})
	case "register":
		if c.Op == "read" {
			return registers.Read(ctx, c.Resource, c.Index)
		}
		data := &p4v1.P4Data{}
		if err := protojson.Unmarshal(c.Data, data); err != nil {
			return nil, err
		}
		return nil, registers.WriteData(ctx, c.Resource, c.Index, data)
	}
	panic(c.Kind)
}
