package main

import (
	"context"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"github.com/zhh2001/p4runtime-go-controller/v2/client"
	"github.com/zhh2001/p4runtime-go-controller/v2/pipeline"
	"github.com/zhh2001/p4runtime-go-controller/v2/tableentry"
)

const needsApplicationArbitration = false

var sdk *client.Client
var pipe *pipeline.Pipeline

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
	return func() { _ = sdk.Close() }, nil
}

func nativeDo(ctx context.Context, c Case) (any, error) {
	if c.ActionKind == "member" || c.ActionKind == "group" {
		return nil, unexpressible("the direct-action Builder does not construct profile references")
	}
	b := tableentry.NewBuilder(pipe, c.Table).Priority(c.Priority).IdleTimeout(c.TTL)
	for _, v := range c.Matches {
		if v.Kind == "exact" {
			b.Match(v.Name, tableentry.Exact(unhex(v.Value)))
		} else {
			b.Match(v.Name, tableentry.Ternary(unhex(v.Value), unhex(v.Mask)))
		}
	}
	if c.Default {
		b.AsDefault()
	}
	if c.ActionKind == "direct" {
		var params []tableentry.ActionParam
		for _, v := range c.Params {
			params = append(params, tableentry.Param(v.Name, unhex(v.Value)))
		}
		b.Action(c.Action, params...)
	}
	var entry *p4v1.TableEntry
	var err error
	if c.Update == "DELETE" || c.Default && c.ActionKind == "none" {
		entry, err = b.BuildKey()
	} else {
		entry, err = b.Build()
	}
	if err != nil {
		return nil, err
	}
	return nil, sdk.Write(ctx, client.WriteOptions{}, &p4v1.Update{Type: updateType(c), Entity: &p4v1.Entity{Entity: &p4v1.Entity_TableEntry{TableEntry: entry}}})
}
