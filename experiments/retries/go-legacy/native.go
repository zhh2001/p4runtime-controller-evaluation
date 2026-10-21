package main

import (
	"context"
	legacy "github.com/antoninbas/p4runtime-go-client/pkg/client"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const needsApplicationArbitration = true

var old *legacy.Client

func mustStatus(err error) []byte {
	b, e := proto.Marshal(status.Convert(err).Proto())
	if e != nil {
		panic(e)
	}
	return b
}
func nativeSetup(ctx context.Context, address string) (func(), error) {
	old = legacy.NewClient(rpc, 1, &p4v1.Uint128{Low: 1})
	return func() {}, nil
}
func nativeWrite(ctx context.Context, updates []*p4v1.Update) (error, []Item) {
	_, err := old.Write(ctx, &p4v1.WriteRequest{DeviceId: 1, ElectionId: &p4v1.Uint128{Low: 1}, Updates: updates})
	return err, nil
}
