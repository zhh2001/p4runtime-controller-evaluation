package main

import (
	"context"
	p4r "github.com/p4lang/p4runtime/go/p4/v1"
	"github.com/zhh2001/p4runtime-go-controller/v2/client"
	"github.com/zhh2001/p4runtime-go-controller/v2/counter"
	"github.com/zhh2001/p4runtime-go-controller/v2/packetio"
	"github.com/zhh2001/p4runtime-go-controller/v2/pipeline"
	"github.com/zhh2001/p4runtime-go-controller/v2/tableentry"
)

var sdk *client.Client
var pipe *pipeline.Pipeline
var subscriber *packetio.Subscriber
var reader *counter.Reader

func nativeOpen(ctx context.Context, address string, data []byte) (func(), error) {
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
	subscriber, err = packetio.NewSubscriber(sdk, pipe)
	if err != nil {
		sdk.Close()
		return nil, err
	}
	reader, err = counter.NewReader(sdk, pipe)
	if err != nil {
		sdk.Close()
		return nil, err
	}
	stop := subscriber.OnPacket(func(_ context.Context, p *packetio.PacketIn) {
		var port uint32
		for _, v := range p.Metadata["ingress_port"] {
			port = port*256 + uint32(v)
		}
		packet(p.Payload, port)
	})
	return func() { stop(); sdk.Close() }, nil
}
func nativeEntry(e entry) (*p4r.TableEntry, error) {
	return tableentry.NewBuilder(pipe, "MyIngress.t_l2").Match("hdr.eth.dst", tableentry.Exact(mac(e.MAC))).Action("MyIngress.forward", tableentry.Param("port", number(e.Port))).Build()
}
func nativeWrite(ctx context.Context, u []*p4r.Update) error {
	return sdk.Write(ctx, client.WriteOptions{}, u...)
}
func nativeCounter(ctx context.Context, index int64) (int64, int64, error) {
	r, err := reader.Read(ctx, "MyIngress.pkt_counter", index)
	if err != nil {
		return 0, 0, err
	}
	return r[0].Packets, r[0].Bytes, nil
}
func nativeSend(ctx context.Context, payload []byte, port uint32) error {
	return subscriber.Send(ctx, &packetio.PacketOut{Payload: payload, Metadata: map[string][]byte{"egress_port": number(port), "_pad": {0}}})
}
