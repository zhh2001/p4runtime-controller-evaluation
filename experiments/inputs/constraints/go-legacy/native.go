package main

import (
	"context"
	legacy "github.com/antoninbas/p4runtime-go-client/pkg/client"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"time"
)

const needsApplicationArbitration = true

var old *legacy.Client

func nativeSetup(ctx context.Context, address string, data []byte) (func(), error) {
	old = legacy.NewClient(rpc, 1, &p4v1.Uint128{Low: 1})
	_, err := old.GetFwdPipe(ctx, legacy.GetFwdPipeP4InfoAndCookie)
	return func() {}, err
}

func nativeDo(ctx context.Context, c Case) (any, error) {
	mfs := map[string]legacy.MatchInterface{}
	for _, v := range c.Matches {
		if v.Kind == "exact" {
			mfs[v.Name] = &legacy.ExactMatch{Value: unhex(v.Value)}
		} else {
			mfs[v.Name] = &legacy.TernaryMatch{Value: unhex(v.Value), Mask: unhex(v.Mask)}
		}
	}
	if c.Default {
		if len(c.Matches) > 0 {
			return nil, unexpressible("default flag is inferred from a nil match map and cannot retain stale match fields")
		}
		mfs = nil
	}
	var action *p4v1.TableAction
	switch c.ActionKind {
	case "direct":
		var params [][]byte
		for _, v := range c.Params {
			params = append(params, unhex(v.Value))
		}
		action = old.NewTableActionDirect(c.Action, params)
	case "member":
		action = old.NewTableActionMember(c.Reference)
	case "group":
		action = old.NewTableActionGroup(c.Reference)
	}
	entry := old.NewTableEntry(c.Table, mfs, action, &legacy.TableEntryOptions{Priority: c.Priority, IdleTimeout: time.Duration(c.TTL)})
	return nil, old.WriteUpdate(ctx, &p4v1.Update{Type: updateType(c), Entity: &p4v1.Entity{Entity: &p4v1.Entity_TableEntry{TableEntry: entry}}})
}
