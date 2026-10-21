package main

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"flag"
	"io"
	"os"
	"time"

	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
)

var rpc p4v1.P4RuntimeClient

type Command struct {
	Op      string            `json:"op"`
	Token   string            `json:"token"`
	Updates []json.RawMessage `json:"updates"`
}
type Item struct {
	Index int   `json:"index"`
	Code  int32 `json:"code"`
}

func main() {
	address := flag.String("address", "", "")
	profile := flag.String("profile", "", "")
	flag.Parse()
	conn, err := grpc.Dial(*address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		panic(err)
	}
	defer conn.Close()
	rpc = p4v1.NewP4RuntimeClient(conn)
	raw := *profile == "raw-current" || *profile == "raw-legacy"
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	if raw || needsApplicationArbitration {
		streamCtx, streamCancel := context.WithCancel(context.Background())
		defer streamCancel()
		stream, e := rpc.StreamChannel(streamCtx)
		if e != nil {
			panic(e)
		}
		e = stream.Send(&p4v1.StreamMessageRequest{Update: &p4v1.StreamMessageRequest_Arbitration{Arbitration: &p4v1.MasterArbitrationUpdate{DeviceId: 1, ElectionId: &p4v1.Uint128{Low: 1}}}})
		if e != nil {
			panic(e)
		}
		response, e := stream.Recv()
		if e != nil || response.GetArbitration().GetStatus().GetCode() != 0 {
			panic("arbitration failed")
		}
	}
	if !raw {
		close, e := nativeSetup(ctx, *address)
		if e != nil {
			panic(e)
		}
		defer close()
	}
	cancel()
	encoder := json.NewEncoder(os.Stdout)
	encoder.Encode(map[string]any{"ready": true, "profile": *profile})
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 65536), 1<<20)
	for scanner.Scan() {
		var command Command
		if err = json.Unmarshal(scanner.Bytes(), &command); err != nil {
			panic(err)
		}
		if command.Op == "close" {
			break
		}
		callCtx, stop := context.WithTimeout(context.Background(), 5*time.Second)
		row := map[string]any{"token": command.Token, "rpc_code": 0, "native_results": nil}
		if command.Op == "read" {
			stream, e := rpc.Read(callCtx, &p4v1.ReadRequest{DeviceId: 1, Entities: []*p4v1.Entity{{Entity: &p4v1.Entity_TableEntry{TableEntry: &p4v1.TableEntry{}}}}})
			if e != nil {
				panic(e)
			}
			entities := []json.RawMessage{}
			for {
				response, e := stream.Recv()
				if e == io.EOF {
					break
				}
				if e != nil {
					panic(e)
				}
				for _, entity := range response.Entities {
					data, e := protojson.MarshalOptions{UseProtoNames: true}.Marshal(entity)
					if e != nil {
						panic(e)
					}
					entities = append(entities, data)
				}
			}
			row["entities"] = entities
		} else {
			updates := []*p4v1.Update{}
			for _, value := range command.Updates {
				update := &p4v1.Update{}
				if e := protojson.Unmarshal(value, update); e != nil {
					panic(e)
				}
				updates = append(updates, update)
			}
			var items []Item
			if raw {
				_, err = rpc.Write(callCtx, &p4v1.WriteRequest{DeviceId: 1, ElectionId: &p4v1.Uint128{Low: 1}, Updates: updates})
			} else {
				err, items = nativeWrite(callCtx, updates)
			}
			row["native_results"] = items
			row["rpc_code"] = int(status.Code(err))
			if err != nil {
				row["error"] = err.Error()
				row["raw_status"] = base64.StdEncoding.EncodeToString(mustStatus(err))
			}
		}
		stop()
		encoder.Encode(row)
	}
	if err = scanner.Err(); err != nil {
		panic(err)
	}
}
