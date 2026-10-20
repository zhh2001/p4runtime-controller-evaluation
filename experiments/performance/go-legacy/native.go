package main

import (
	"context"

	legacy "github.com/antoninbas/p4runtime-go-client/pkg/client"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
)

var old *legacy.Client

func nativeSetup(ctx context.Context, address string, data []byte) (func(), error) {
	cleanup, err := rawSetup(ctx, address)
	if err != nil {
		return nil, err
	}
	old = legacy.NewClient(rpc, 1, &p4v1.Uint128{Low: 1})
	if _, err = old.GetFwdPipe(ctx, legacy.GetFwdPipeP4InfoAndCookie); err != nil {
		cleanup()
		return nil, err
	}
	return cleanup, nil
}

func nativeEntry(key, port uint32) (*p4v1.TableEntry, error) {
	action := old.NewTableActionDirect("MyIngress.forward", [][]byte{number(port)})
	item := old.NewTableEntry("MyIngress.forwarding", map[string]legacy.MatchInterface{
		"h.key.value": &legacy.ExactMatch{Value: number(key)},
	}, action, nil)
	item.Metadata = []byte("E2")
	return item, nil
}

func nativeWrite(ctx context.Context, updates []*p4v1.Update) error {
	_, err := old.Write(ctx, &p4v1.WriteRequest{DeviceId: 1, ElectionId: &p4v1.Uint128{Low: 1}, Updates: updates})
	return err
}

func nativeRead(ctx context.Context, selector *p4v1.Entity) ([]*p4v1.Entity, error) {
	if len(selector.GetTableEntry().Match) != 0 {
		item, err := old.ReadEntitySingle(ctx, selector)
		if err != nil {
			return nil, err
		}
		return []*p4v1.Entity{item}, nil
	}
	items := make(chan *p4v1.Entity, 128)
	finished := make(chan error, 1)
	go func() { finished <- old.ReadEntityWildcard(ctx, selector, items) }()
	var result []*p4v1.Entity
	for item := range items {
		result = append(result, item)
	}
	return result, <-finished
}
