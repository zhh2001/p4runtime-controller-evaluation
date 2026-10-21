package main

import (
	"context"

	legacy "github.com/antoninbas/p4runtime-go-client/pkg/client"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
)

const needsApplicationArbitration = true

var old *legacy.Client

func nativeSetup(ctx context.Context, address string, data []byte) (func(), error) {
	old = legacy.NewClient(rpc, 1, &p4v1.Uint128{Low: 1})
	_, err := old.GetFwdPipe(ctx, legacy.GetFwdPipeP4InfoAndCookie)
	return func() {}, err
}

func nativeDo(ctx context.Context, c Case) (any, error) {
	switch c.Kind {
	case "match", "param":
		action := old.NewTableActionDirect(c.Action, [][]byte{unhex(c.ActionValue)})
		entry := old.NewTableEntry(c.Table, map[string]legacy.MatchInterface{c.Field: &legacy.ExactMatch{Value: unhex(c.Key)}}, action, nil)
		return nil, old.WriteUpdate(ctx, &p4v1.Update{Type: p4v1.Update_MODIFY, Entity: &p4v1.Entity{Entity: &p4v1.Entity_TableEntry{TableEntry: entry}}})
	case "counter":
		if c.Op == "read" {
			if c.Index == -1 {
				return old.ReadCounterEntryWildcard(ctx, c.Resource)
			}
			return old.ReadCounterEntry(ctx, c.Resource, c.Index)
		}
		return nil, old.ModifyCounterEntry(ctx, c.Resource, c.Index, &p4v1.CounterData{PacketCount: 11, ByteCount: 704})
	case "meter":
		if c.Op == "read" {
			if c.Index == -1 {
				return old.ReadMeterEntryWildcard(ctx, c.Resource)
			}
			return old.ReadMeterEntry(ctx, c.Resource, c.Index)
		}
		return nil, unexpressible("no dedicated meter-write helper in this snapshot")
	case "register":
		return nil, unexpressible("no dedicated register helper in this snapshot")
	}
	panic(c.Kind)
}
