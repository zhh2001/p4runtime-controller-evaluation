module p4runtime-experiments/boundaries-go-legacy

go 1.26.0

require (
	github.com/antoninbas/p4runtime-go-client v0.0.0-20250129182037-de3748d9d859
	github.com/p4lang/p4runtime v1.4.0-rc.5
	google.golang.org/grpc v1.56.3
	google.golang.org/protobuf v1.33.0
)

require (
	github.com/golang/protobuf v1.5.3 // indirect
	github.com/sirupsen/logrus v1.9.3 // indirect
	golang.org/x/net v0.33.0 // indirect
	golang.org/x/sys v0.28.0 // indirect
	golang.org/x/text v0.21.0 // indirect
	google.golang.org/genproto v0.0.0-20230410155749-daa745c078e1 // indirect
)

replace github.com/antoninbas/p4runtime-go-client => ../../../../.runtime/sources/p4runtime-go-client
