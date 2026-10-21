package main

import (
	"context"
	"fmt"
	legacy "github.com/antoninbas/p4runtime-go-client/pkg/client"
	p4r "github.com/p4lang/p4runtime/go/p4/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

var old *legacy.Client

func nativeOpen(ctx context.Context, address string, _ []byte) (func(), error) {
	conn, err := grpc.DialContext(ctx, address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	rpc = p4r.NewP4RuntimeClient(conn)
	old = legacy.NewClient(rpc, 1, &p4r.Uint128{Low: 1})
	stop := make(chan struct{})
	arb := make(chan bool, 8)
	messages := make(chan *p4r.StreamMessageResponse, 64)
	done := make(chan error, 1)
	watch, finish := context.WithCancel(context.Background())
	go func() { done <- old.Run(stop, arb, messages) }()
	go func() {
		for {
			select {
			case m := <-messages:
				if p := m.GetPacket(); p != nil {
					var port uint32
					for _, v := range p.Metadata {
						if v.MetadataId == 1 {
							for _, b := range v.Value {
								port = port*256 + uint32(b)
							}
						}
					}
					packet(p.Payload, port)
				}
			case <-watch.Done():
				return
			}
		}
	}()
	cleanup := func() { finish(); close(stop); conn.Close() }
	select {
	case ok := <-arb:
		if !ok {
			err = fmt.Errorf("not primary")
		}
	case err = <-done:
	case <-ctx.Done():
		err = ctx.Err()
	}
	if err == nil {
		_, err = old.GetFwdPipe(ctx, legacy.GetFwdPipeP4InfoAndCookie)
	}
	if err != nil {
		cleanup()
		return nil, err
	}
	return cleanup, nil
}
func nativeEntry(e entry) (*p4r.TableEntry, error) {
	return old.NewTableEntry("MyIngress.t_l2", map[string]legacy.MatchInterface{"hdr.eth.dst": &legacy.ExactMatch{Value: mac(e.MAC)}}, old.NewTableActionDirect("MyIngress.forward", [][]byte{number(e.Port)}), nil), nil
}
func nativeWrite(ctx context.Context, u []*p4r.Update) error {
	_, err := old.Write(ctx, &p4r.WriteRequest{DeviceId: 1, ElectionId: &p4r.Uint128{Low: 1}, Updates: u})
	return err
}
func nativeCounter(ctx context.Context, index int64) (int64, int64, error) {
	v, err := old.ReadCounterEntry(ctx, "MyIngress.pkt_counter", index)
	if err != nil {
		return 0, 0, err
	}
	return v.GetPacketCount(), v.GetByteCount(), nil
}
func nativeSend(ctx context.Context, payload []byte, port uint32) error {
	return old.SendPacketOut(ctx, &p4r.PacketOut{Payload: payload, Metadata: metadata(port)})
}
