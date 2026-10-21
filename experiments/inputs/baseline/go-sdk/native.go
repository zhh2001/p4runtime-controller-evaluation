package main

import (
	"context"
	"errors"

	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"github.com/zhh2001/p4runtime-go-controller/v2/client"
	sdkerrors "github.com/zhh2001/p4runtime-go-controller/v2/errors"
	"github.com/zhh2001/p4runtime-go-controller/v2/pipeline"
	"github.com/zhh2001/p4runtime-go-controller/v2/tableentry"
)

func nativeSetup(ctx context.Context, addr string) error { return nil }

func nativeBuild(c Case) (*p4v1.TableEntry, error) {
	p, err := pipeline.New(info, nil)
	if err != nil {
		return nil, err
	}
	b := tableentry.NewBuilder(p, c.Table).Priority(c.Priority)
	for _, m := range c.Matches {
		var v tableentry.MatchValue
		switch m.Kind {
		case "exact":
			v = tableentry.Exact(unhex(m.Value))
		case "lpm":
			v = tableentry.LPM(unhex(m.Value), m.Prefix)
		case "ternary":
			v = tableentry.Ternary(unhex(m.Value), unhex(m.Mask))
		case "range":
			v = tableentry.Range(unhex(m.Value), unhex(m.High))
		case "optional":
			v = tableentry.Optional(unhex(m.Value))
		}
		b.Match(m.Field, v)
	}
	params := []tableentry.ActionParam{}
	for name, value := range c.Params {
		params = append(params, tableentry.Param(name, unhex(value)))
	}
	return b.Action(c.Action, params...).Build()
}

func nativeWrite(ctx context.Context, addr string, updates []*p4v1.Update) (error, []Item, bool) {
	c, err := client.Dial(ctx, addr, client.WithInsecure(), client.WithDeviceID(1), client.WithElectionID(client.ElectionID{Low: 1}))
	if err != nil {
		return err, nil, false
	}
	defer c.Close()
	if err = c.BecomePrimary(ctx); err != nil {
		return err, nil, false
	}
	err = c.Write(ctx, client.WriteOptions{}, updates...)
	var detail *sdkerrors.WriteError
	var exposed []Item
	if errors.As(err, &detail) && detail.Updates != nil {
		exposed = []Item{}
		for i, item := range detail.Updates {
			exposed = append(exposed, Item{Index: i, Code: item.CanonicalCode})
		}
	}
	return err, exposed, errors.Is(err, sdkerrors.ErrEntryExists)
}
