package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"runtime"
	"sync"
	"time"

	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"github.com/zhh2001/p4runtime-go-controller/v2/client"
	sdkerrors "github.com/zhh2001/p4runtime-go-controller/v2/errors"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc/status"
)

func mono() int64 {
	var value unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_MONOTONIC, &value); err != nil {
		panic(err)
	}
	return value.Nano()
}

func integer(row map[string]any, key string) int {
	if value, ok := row[key].(float64); ok {
		return int(value)
	}
	return 0
}

func result(err error) map[string]any {
	row := map[string]any{"rpc_code": int(status.Code(err)), "cancelled": errors.Is(err, context.Canceled),
		"deadline": errors.Is(err, context.DeadlineExceeded), "not_primary": errors.Is(err, sdkerrors.ErrNotPrimary),
		"stream_closed": errors.Is(err, sdkerrors.ErrStreamClosed)}
	if err != nil {
		row["error"] = err.Error()
	}
	return row
}

func main() {
	address := flag.String("address", "", "P4Runtime endpoint")
	_ = flag.String("p4info", "", "shared schema")
	path := flag.String("path", "native", "native or queued")
	delay := flag.Int("delay-ms", 0, "per-packet processing delay")
	capacity := flag.Int("capacity", 50, "application queue capacity")
	flag.Parse()
	var outputMu sync.Mutex
	encoder := json.NewEncoder(os.Stdout)
	emit := func(row map[string]any) {
		outputMu.Lock()
		defer outputMu.Unlock()
		row["monotonic_ns"] = mono()
		if err := encoder.Encode(row); err != nil {
			panic(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	c, err := client.Dial(ctx, *address, client.WithInsecure(), client.WithDeviceID(1),
		client.WithElectionID(client.ElectionID{Low: 1}))
	cancel()
	if err != nil {
		emit(map[string]any{"kind": "ready", "error": err.Error()})
		return
	}
	defer c.Close()
	var statsMu sync.Mutex
	arrivals, completed := []uint64{}, []uint64{}
	arrivalTimes, completeTimes := []int64{}, []int64{}
	dropped, active := 0, 0
	process := func(number uint64) {
		statsMu.Lock()
		active++
		statsMu.Unlock()
		if *delay > 0 {
			time.Sleep(time.Duration(*delay) * time.Millisecond)
		}
		statsMu.Lock()
		active--
		completed = append(completed, number)
		completeTimes = append(completeTimes, mono())
		statsMu.Unlock()
	}
	queue := make(chan uint64, *capacity)
	workerCtx, stopWorker := context.WithCancel(context.Background())
	var workers sync.WaitGroup
	if *path == "queued" {
		workers.Go(func() {
			for {
				select {
				case number := <-queue:
					process(number)
				case <-workerCtx.Done():
					return
				}
			}
		})
	}
	c.OnPacketIn(func(_ context.Context, packet *p4v1.PacketIn) {
		number := binary.BigEndian.Uint64(packet.Payload[8:16])
		statsMu.Lock()
		arrivals = append(arrivals, number)
		arrivalTimes = append(arrivalTimes, mono())
		statsMu.Unlock()
		if *path == "native" {
			process(number)
		} else {
			select {
			case queue <- number:
			default:
				statsMu.Lock()
				dropped++
				statsMu.Unlock()
			}
		}
	})
	statistics := func() map[string]any {
		statsMu.Lock()
		defer statsMu.Unlock()
		return map[string]any{"is_primary": c.IsPrimary(), "state": c.State().String(),
			"arrived": append([]uint64{}, arrivals...), "processed": append([]uint64{}, completed...),
			"arrival_times_ns": append([]int64{}, arrivalTimes...), "completion_times_ns": append([]int64{}, completeTimes...),
			"application_dropped": dropped, "in_progress": active, "queue_depth": len(queue),
			"goroutines": runtime.NumGoroutine()}
	}
	var jobsMu sync.Mutex
	jobs := map[int]context.CancelFunc{}
	var sends sync.WaitGroup
	emit(map[string]any{"kind": "ready", "is_primary": c.IsPrimary(), "path": *path, "delay_ms": *delay})
	decoder := json.NewDecoder(os.Stdin)
	for {
		var command map[string]any
		if err := decoder.Decode(&command); err == io.EOF {
			break
		} else if err != nil {
			panic(err)
		}
		row := map[string]any{"kind": "reply", "id": integer(command, "id"), "op": command["op"]}
		switch command["op"] {
		case "state", "stats":
			for key, value := range statistics() {
				row[key] = value
			}
		case "start-send":
			job, size := integer(command, "job"), integer(command, "size")
			jobCtx, stop := context.WithCancel(context.Background())
			jobsMu.Lock()
			jobs[job] = stop
			jobsMu.Unlock()
			if value, _ := command["pre_cancelled"].(bool); value {
				stop()
			}
			sends.Go(func() {
				defer stop()
				payload := make([]byte, size)
				copy(payload, []byte("P4EXP001"))
				binary.BigEndian.PutUint64(payload[8:16], uint64(job))
				emit(map[string]any{"kind": "send_started", "job": job})
				err := c.SendPacketOut(jobCtx, &p4v1.PacketOut{Payload: payload,
					Metadata: []*p4v1.PacketMetadata{{MetadataId: 1, Value: []byte{1}}}})
				out := result(err)
				out["kind"], out["job"] = "send_result", job
				emit(out)
				jobsMu.Lock()
				delete(jobs, job)
				jobsMu.Unlock()
			})
		case "cancel":
			jobsMu.Lock()
			stop := jobs[integer(command, "job")]
			jobsMu.Unlock()
			if stop != nil {
				stop()
			}
		case "start-close":
			job := integer(command, "job")
			graceful, _ := command["graceful"].(bool)
			timeout := integer(command, "timeout_ms")
			sends.Go(func() {
				var err error
				if graceful {
					ctx, stop := context.WithTimeout(context.Background(), time.Duration(timeout)*time.Millisecond)
					err = c.CloseGracefully(ctx)
					stop()
				} else {
					err = c.Close()
				}
				out := result(err)
				out["kind"], out["job"] = "close_result", job
				out["is_primary"], out["state"] = c.IsPrimary(), c.State().String()
				emit(out)
			})
		case "close":
			jobsMu.Lock()
			for _, stop := range jobs {
				stop()
			}
			jobsMu.Unlock()
			_ = c.Close()
			stopWorker()
			sends.Wait()
			workers.Wait()
			row["goroutines_after_close"] = runtime.NumGoroutine()
			emit(row)
			return
		default:
			row["fatal"] = "unknown command"
		}
		emit(row)
	}
	stopWorker()
	_ = c.Close()
	sends.Wait()
	workers.Wait()
}
