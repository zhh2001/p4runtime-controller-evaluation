package main

import (
	"context"
	"errors"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"github.com/zhh2001/p4runtime-go-controller/v2/client"
	sdkerrors "github.com/zhh2001/p4runtime-go-controller/v2/errors"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const needsApplicationArbitration = false

var sdk *client.Client

func mustStatus(err error) []byte {
	b, e := proto.Marshal(status.Convert(err).Proto())
	if e != nil {
		panic(e)
	}
	return b
}
func nativeSetup(ctx context.Context, address string) (func(), error) {
	var err error
	sdk, err = client.Dial(ctx, address, client.WithInsecure(), client.WithDeviceID(1), client.WithElectionID(client.ElectionID{Low: 1}))
	if err != nil {
		return nil, err
	}
	if err = sdk.BecomePrimary(ctx); err != nil {
		sdk.Close()
		return nil, err
	}
	return func() { sdk.Close() }, nil
}
func nativeWrite(ctx context.Context, updates []*p4v1.Update) (error, []Item) {
	err := sdk.Write(ctx, client.WriteOptions{}, updates...)
	var detail *sdkerrors.WriteError
	var items []Item
	if errors.As(err, &detail) && detail.Updates != nil {
		items = []Item{}
		for i, v := range detail.Updates {
			items = append(items, Item{Index: i, Code: v.CanonicalCode})
		}
	}
	return err, items
}
