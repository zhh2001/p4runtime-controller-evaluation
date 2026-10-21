module p4runtime-experiments/e5-go-sdk

go 1.26.0

require (
	github.com/p4lang/p4runtime v1.5.0
	github.com/zhh2001/p4runtime-go-controller/v2 v2.0.1
	google.golang.org/grpc v1.83.2
	google.golang.org/protobuf v1.36.11
)

require (
	golang.org/x/net v0.60.0 // indirect
	golang.org/x/sys v0.48.0 // indirect
	golang.org/x/text v0.42.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260526163538-3dc84a4a5aaa // indirect
)

replace github.com/zhh2001/p4runtime-go-controller/v2 => ../../../.runtime/sources/p4runtime-go-controller
