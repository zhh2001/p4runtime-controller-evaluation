package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	p4i "github.com/p4lang/p4runtime/go/p4/config/v1"
	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/encoding/prototext"
)

type Command struct {
	Op          string
	Operation   string
	N           int
	Batch       int
	Concurrency int
	Seconds     float64
	Phase       string
}

type Record struct {
	Worker         int    "json:\"worker\""
	Iteration      int    "json:\"iteration\""
	First          int    "json:\"first\""
	Count          int    "json:\"count\""
	Port           int    "json:\"port\""
	Admission      int64  "json:\"admission_ns\""
	Start          int64  "json:\"start_ns\""
	BuildEnd       int64  "json:\"build_end_ns\""
	End            int64  "json:\"end_ns\""
	CallMade       bool   "json:\"call_made\""
	Code           int    "json:\"rpc_code\""
	Error          string "json:\"error,omitempty\""
	ReadCount      int    "json:\"read_count\""
	KeySum         uint64 "json:\"key_sum\""
	PortSum        uint64 "json:\"port_sum\""
	OKUpdates      int    "json:\"ok_updates\""
	FailedUpdates  int    "json:\"failed_updates\""
	UnknownUpdates int    "json:\"unknown_updates\""
}

var rpc p4v1.P4RuntimeClient
var tableID, fieldID, actionID, paramID uint32
var raw bool

func writeCounts(err error, count int, called bool) (ok, failed, unknown int) {
	if err == nil {
		return count, 0, 0
	}
	if !called {
		return 0, count, 0
	}
	state := status.Convert(err)
	details := state.Proto().Details
	if state.Code() != codes.Unknown || len(details) != count {
		return 0, 0, count
	}
	for _, detail := range details {
		value := &p4v1.Error{}
		if detail.UnmarshalTo(value) != nil || value.CanonicalCode < 0 || value.CanonicalCode > 16 {
			return 0, 0, count
		}
		if value.CanonicalCode == 0 {
			ok++
		} else {
			failed++
		}
	}
	if failed == 0 {
		return 0, 0, count
	}
	return ok, failed, 0
}

func mono() int64 {
	var value unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_MONOTONIC, &value); err != nil {
		panic(err)
	}
	return value.Nano()
}

func number(value uint32) []byte {
	var buffer [4]byte
	binary.BigEndian.PutUint32(buffer[:], value)
	first := 0
	for first < 3 && buffer[first] == 0 {
		first++
	}
	return append([]byte(nil), buffer[first:]...)
}

func rawEntry(key, port uint32) *p4v1.TableEntry {
	return &p4v1.TableEntry{TableId: tableID, Match: []*p4v1.FieldMatch{
		{FieldId: fieldID, FieldMatchType: &p4v1.FieldMatch_Exact_{Exact: &p4v1.FieldMatch_Exact{Value: number(key)}}},
	}, Action: &p4v1.TableAction{Type: &p4v1.TableAction_Action{Action: &p4v1.Action{
		ActionId: actionID, Params: []*p4v1.Action_Param{{ParamId: paramID, Value: number(port)}},
	}}}, Metadata: []byte("E2")}
}

func rawSetup(ctx context.Context, address string) (func(), error) {
	connection, err := grpc.DialContext(ctx, address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	rpc = p4v1.NewP4RuntimeClient(connection)
	streamCtx, stop := context.WithCancel(context.Background())
	stream, err := rpc.StreamChannel(streamCtx)
	if err != nil {
		stop()
		_ = connection.Close()
		return nil, err
	}
	err = stream.Send(&p4v1.StreamMessageRequest{Update: &p4v1.StreamMessageRequest_Arbitration{
		Arbitration: &p4v1.MasterArbitrationUpdate{DeviceId: 1, ElectionId: &p4v1.Uint128{Low: 1}},
	}})
	if err == nil {
		var response *p4v1.StreamMessageResponse
		response, err = stream.Recv()
		if err == nil && response.GetArbitration().GetStatus().GetCode() != 0 {
			err = errors.New("raw arbitration rejected")
		}
	}
	if err != nil {
		stop()
		_ = connection.Close()
		return nil, err
	}
	closed := make(chan struct{})
	go func() {
		defer close(closed)
		for {
			if _, err := stream.Recv(); err != nil {
				return
			}
		}
	}()
	return func() { stop(); _ = connection.Close(); <-closed }, nil
}

func rawRead(ctx context.Context, selector *p4v1.Entity) ([]*p4v1.Entity, error) {
	call, err := rpc.Read(ctx, &p4v1.ReadRequest{DeviceId: 1, Entities: []*p4v1.Entity{selector}})
	if err != nil {
		return nil, err
	}
	var entries []*p4v1.Entity
	for {
		response, err := call.Recv()
		if errors.Is(err, io.EOF) {
			return entries, nil
		}
		if err != nil {
			return entries, err
		}
		entries = append(entries, response.Entities...)
	}
}

func apply(command Command, worker, iteration, chunk int) Record {
	row := Record{Worker: worker, Iteration: iteration, First: chunk*command.Batch + 1,
		Count: command.Batch, Port: 2, Start: mono()}
	write := command.Operation == "insert" || command.Operation == "modify"
	var updates []*p4v1.Update
	var selector *p4v1.Entity
	var err error
	if write {
		if command.Operation == "insert" {
			row.Port = 1
		}
		if command.Operation == "modify" && command.Seconds > 0 {
			row.Port = 2 - (iteration / (command.N / command.Batch / command.Concurrency) % 2)
		}
		updates = make([]*p4v1.Update, 0, row.Count)
		for key := row.First; key < row.First+row.Count; key++ {
			var item *p4v1.TableEntry
			if raw {
				item = rawEntry(uint32(key), uint32(row.Port))
			} else {
				item, err = nativeEntry(uint32(key), uint32(row.Port))
			}
			if err != nil {
				break
			}
			kind := p4v1.Update_MODIFY
			if command.Operation == "insert" {
				kind = p4v1.Update_INSERT
			}
			updates = append(updates, &p4v1.Update{Type: kind, Entity: &p4v1.Entity{
				Entity: &p4v1.Entity_TableEntry{TableEntry: item},
			}})
		}
	} else {
		item := &p4v1.TableEntry{TableId: tableID}
		row.First, row.Count, row.Port = 0, command.N, 0
		if command.Operation == "read_one" {
			row.First, row.Count = 1+(worker+iteration*command.Concurrency)%command.N, 1
			item.Match = rawEntry(uint32(row.First), 1).Match
		}
		selector = &p4v1.Entity{Entity: &p4v1.Entity_TableEntry{TableEntry: item}}
	}
	row.BuildEnd = mono()
	var entities []*p4v1.Entity
	if err == nil {
		row.CallMade = true
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		if write {
			if raw {
				_, err = rpc.Write(ctx, &p4v1.WriteRequest{DeviceId: 1,
					ElectionId: &p4v1.Uint128{Low: 1}, Updates: updates})
			} else {
				err = nativeWrite(ctx, updates)
			}
		} else {
			if raw {
				entities, err = rawRead(ctx, selector)
			} else {
				entities, err = nativeRead(ctx, selector)
			}
		}
		cancel()
	}
	row.End = mono()
	row.Code = int(status.Code(err))
	if err != nil {
		row.Error = err.Error()
	}
	if write {
		row.OKUpdates, row.FailedUpdates, row.UnknownUpdates = writeCounts(err, row.Count, row.CallMade)
	} else if err == nil {
		for _, entity := range entities {
			item := entity.GetTableEntry()
			if item == nil || item.IsDefaultAction {
				continue
			}
			row.ReadCount++
			for _, match := range item.Match {
				var key uint64
				for _, value := range match.GetExact().GetValue() {
					key = key<<8 | uint64(value)
				}
				row.KeySum += key
			}
			for _, param := range item.GetAction().GetAction().GetParams() {
				var port uint64
				for _, value := range param.Value {
					port = port<<8 | uint64(value)
				}
				row.PortSum += port
			}
		}
	}
	return row
}

func run(command Command) map[string]any {
	if command.N <= 0 || command.N > 32768 || command.Batch <= 0 ||
		command.N%command.Batch != 0 || command.Concurrency <= 0 || command.Seconds < 0 ||
		command.N/command.Batch%command.Concurrency != 0 {
		return map[string]any{"fatal": "invalid workload partition"}
	}
	if command.Operation != "insert" && command.Operation != "modify" &&
		command.Operation != "read_one" && command.Operation != "read_all" {
		return map[string]any{"fatal": "unknown operation"}
	}
	gate := make(chan struct{})
	var ready, done sync.WaitGroup
	parts := make([][]Record, command.Concurrency)
	var started, stop int64
	ready.Add(command.Concurrency)
	done.Add(command.Concurrency)
	for worker := 0; worker < command.Concurrency; worker++ {
		go func(worker int) {
			defer done.Done()
			ready.Done()
			<-gate
			chunks := command.N / command.Batch
			for iteration := 0; ; iteration++ {
				admission := mono()
				if command.Seconds > 0 {
					if admission >= stop {
						break
					}
				} else {
					limit := chunks
					if command.Operation == "read_one" || command.Operation == "read_all" {
						limit = command.Concurrency
					}
					if worker+iteration*command.Concurrency >= limit {
						break
					}
				}
				chunk := (worker + iteration*command.Concurrency) % chunks
				record := apply(command, worker, iteration, chunk)
				record.Admission = admission
				parts[worker] = append(parts[worker], record)
			}
		}(worker)
	}
	ready.Wait()
	started = mono()
	stop = started + int64(command.Seconds*1e9)
	close(gate)
	done.Wait()
	finished := mono()
	records := []Record{}
	for _, part := range parts {
		records = append(records, part...)
	}
	sort.Slice(records, func(i, j int) bool { return records[i].Start < records[j].Start })
	attempts, calls, successes, okUpdates, failedUpdates, unresolved := len(records), 0, 0, 0, 0, 0
	for _, record := range records {
		if record.CallMade {
			calls++
		}
		if record.Code == 0 {
			successes++
		}
		okUpdates += record.OKUpdates
		failedUpdates += record.FailedUpdates
		unresolved += record.UnknownUpdates
	}
	return map[string]any{"operation": command.Operation, "phase": command.Phase, "n": command.N,
		"batch": command.Batch, "concurrency": command.Concurrency, "requested_seconds": command.Seconds,
		"start_ns": started, "scheduled_stop_ns": stop, "end_ns": finished, "elapsed_ns": finished - started,
		"attempts": attempts, "rpc_calls": calls, "successful_calls": successes, "failed_calls": attempts - successes,
		"ok_updates": okUpdates, "failed_updates": failedUpdates, "unknown_updates": unresolved, "records": records}
}

func main() {
	address := flag.String("address", "", "P4Runtime endpoint")
	profile := flag.String("profile", "", "adapter profile")
	infoPath := flag.String("p4info", "", "shared P4Info")
	flag.Parse()
	data, err := os.ReadFile(*infoPath)
	if err != nil {
		panic(err)
	}
	info := &p4i.P4Info{}
	if err = prototext.Unmarshal(data, info); err != nil {
		panic(err)
	}
	for _, table := range info.Tables {
		if table.Preamble.Name == "MyIngress.forwarding" {
			tableID, fieldID = table.Preamble.Id, table.MatchFields[0].Id
		}
	}
	for _, action := range info.Actions {
		if action.Preamble.Name == "MyIngress.forward" {
			actionID, paramID = action.Preamble.Id, action.Params[0].Id
		}
	}
	raw = strings.HasPrefix(*profile, "raw-")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	var cleanup func()
	if raw {
		cleanup, err = rawSetup(ctx, *address)
	} else {
		cleanup, err = nativeSetup(ctx, *address, data)
	}
	cancel()
	output := json.NewEncoder(os.Stdout)
	if err != nil {
		_ = output.Encode(map[string]any{"fatal": err.Error()})
		return
	}
	defer func() { cleanup() }()
	samples := []any{}
	for _, pair := range [][2]uint32{{1, 1}, {256, 2}, {0xffffffff, 511}} {
		var item *p4v1.TableEntry
		if raw {
			item = rawEntry(pair[0], pair[1])
		} else {
			item, err = nativeEntry(pair[0], pair[1])
			if err != nil {
				panic(err)
			}
		}
		wire, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(item)
		if err != nil {
			panic(err)
		}
		samples = append(samples, json.RawMessage(wire))
	}
	_ = output.Encode(map[string]any{"ready": true, "profile": *profile, "wire_samples": samples, "rpc_deadline_seconds": 10})
	input := json.NewDecoder(os.Stdin)
	for {
		var command Command
		if err = input.Decode(&command); err == io.EOF {
			return
		} else if err != nil {
			panic(err)
		}
		if command.Op == "close" {
			cleanup()
			cleanup = func() {}
			_ = output.Encode(map[string]any{"closed": true})
			return
		}
		if err = output.Encode(run(command)); err != nil {
			panic(err)
		}
	}
}
