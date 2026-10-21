package construction

import (
	"testing"

	"github.com/zhh2001/p4runtime-go-controller/v2/codec"
	"google.golang.org/protobuf/proto"
)

func BenchmarkComponents(b *testing.B) {
	inputs := readInputs(b)
	if err := setupNative(); err != nil {
		b.Fatal(err)
	}
	for _, workload := range []string{"exact", "mixed"} {
		input := inputs[workload][0]
		b.Run("table_lookup/"+workload, func(b *testing.B) {
			b.ReportAllocs()
			for b.Loop() {
				result, ok := pipe.Table(input.Table)
				if !ok || result == nil {
					b.Fatal("table not found")
				}
			}
			reportElapsed(b)
		})
		b.Run("action_lookup/"+workload, func(b *testing.B) {
			b.ReportAllocs()
			for b.Loop() {
				result, ok := pipe.Action(input.Action)
				if !ok || result == nil {
					b.Fatal("action not found")
				}
			}
			reportElapsed(b)
		})
		b.Run("clone_entry/"+workload, func(b *testing.B) {
			entry := rawFactory()(&input)
			b.ReportAllocs()
			for b.Loop() {
				result := proto.Clone(entry)
				if result == nil {
					b.Fatal("nil clone")
				}
			}
			reportElapsed(b)
		})
	}
	b.Run("encode_exact32", func(b *testing.B) {
		value := inputs["exact"][0].Matches[0].Value
		b.ReportAllocs()
		for b.Loop() {
			result, err := codec.EncodeBytes(value, 32)
			if err != nil || result == nil {
				b.Fatal(err)
			}
		}
		reportElapsed(b)
	})
}
