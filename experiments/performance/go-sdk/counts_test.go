package main

import (
	"errors"
	"testing"

	p4v1 "github.com/p4lang/p4runtime/go/p4/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func rich(t *testing.T, values ...int32) error {
	t.Helper()
	state := status.New(codes.Unknown, "batch")
	for _, value := range values {
		var err error
		state, err = state.WithDetails(&p4v1.Error{CanonicalCode: value})
		if err != nil {
			t.Fatal(err)
		}
	}
	return state.Err()
}

func TestWriteOutcomeKnowledge(t *testing.T) {
	cases := []struct {
		name   string
		err    error
		called bool
		want   [3]int
	}{
		{"accepted", nil, true, [3]int{2, 0, 0}},
		{"local_rejection", errors.New("invalid width"), false, [3]int{0, 2, 0}},
		{"unavailable", status.Error(codes.Unavailable, "connection lost"), true, [3]int{0, 0, 2}},
		{"partial_success", rich(t, 0, 6), true, [3]int{1, 1, 0}},
		{"all_failed", rich(t, 6, 8), true, [3]int{0, 2, 0}},
		{"truncated_status", rich(t, 6), true, [3]int{0, 0, 2}},
		{"inconsistent_success", rich(t, 0, 0), true, [3]int{0, 0, 2}},
		{"invalid_code", rich(t, 0, 17), true, [3]int{0, 0, 2}},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			ok, failed, unknown := writeCounts(item.err, 2, item.called)
			if [3]int{ok, failed, unknown} != item.want {
				t.Fatalf("outcome=(%d,%d,%d), want %v", ok, failed, unknown, item.want)
			}
		})
	}
}
