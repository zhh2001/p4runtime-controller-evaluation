package main

import (
	"context"

	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"github.com/zhh2001/p4runtime-go-controller/v2/client"
	"github.com/zhh2001/p4runtime-go-controller/v2/pipeline"
	"github.com/zhh2001/p4runtime-go-controller/v2/tableentry"
)

var sdk *client.Client
var pipe *pipeline.Pipeline

func nativeSetup(ctx context.Context, address string, data []byte) (func(), error) {
	var err error
	pipe, err = pipeline.LoadText(data, nil)
	if err != nil {
		return nil, err
	}
	sdk, err = client.Dial(ctx, address, client.WithInsecure(), client.WithDeviceID(1),
		client.WithElectionID(client.ElectionID{Low: 1}))
	if err != nil {
		return nil, err
	}
	return func() { _ = sdk.Close() }, nil
}

func nativeEntry(key, port uint32) (*p4v1.TableEntry, error) {
	return tableentry.NewBuilder(pipe, "MyIngress.forwarding").
		Match("h.key.value", tableentry.Exact(number(key))).
		Action("MyIngress.forward", tableentry.Param("port", number(port))).
		Metadata([]byte("E2")).Build()
}

func nativeWrite(ctx context.Context, updates []*p4v1.Update) error {
	return sdk.Write(ctx, client.WriteOptions{}, updates...)
}

func nativeRead(ctx context.Context, selector *p4v1.Entity) ([]*p4v1.Entity, error) {
	return sdk.Read(ctx, selector)
}
