package main

import (
	"context"
	"fmt"

	legacy "github.com/antoninbas/p4runtime-go-client/pkg/client"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
)

var old *legacy.Client

func nativeSetup(ctx context.Context, addr string) error {
	old = legacy.NewClient(rpc, 1, &p4v1.Uint128{Low: 1})
	_, err := old.GetFwdPipe(ctx, legacy.GetFwdPipeP4InfoAndCookie)
	return err
}

func nativeBuild(c Case) (*p4v1.TableEntry, error) {
	matches := map[string]legacy.MatchInterface{}
	for _, m := range c.Matches {
		var v legacy.MatchInterface
		switch m.Kind {
		case "exact":
			v = &legacy.ExactMatch{Value: unhex(m.Value)}
		case "lpm":
			v = &legacy.LpmMatch{Value: unhex(m.Value), PLen: m.Prefix}
		case "ternary":
			v = &legacy.TernaryMatch{Value: unhex(m.Value), Mask: unhex(m.Mask)}
		case "range":
			v = &legacy.RangeMatch{Low: unhex(m.Value), High: unhex(m.High)}
		case "optional":
			v = &legacy.OptionalMatch{Value: unhex(m.Value)}
		}
		matches[m.Field] = v
	}
	params := [][]byte{}
	for name, value := range c.Params {
		if name != "port" {
			return nil, fmt.Errorf("UNSUPPORTED: named unknown action parameter is not expressible in the positional API")
		}
		params = append(params, unhex(value))
	}
	action := old.NewTableActionDirect(c.Action, params)
	return old.NewTableEntry(c.Table, matches, action, &legacy.TableEntryOptions{Priority: c.Priority}), nil
}

func nativeWrite(ctx context.Context, addr string, updates []*p4v1.Update) (error, []Item, bool) {
	_, err := old.Write(ctx, &p4v1.WriteRequest{DeviceId: 1, ElectionId: &p4v1.Uint128{Low: 1}, Updates: updates})
	return err, nil, false
}
