// verify — tiny CLI that calls core.VerifyChain against a universe.db
// produced by *any* elastik implementation. Exists so we can prove
// that the Go HMAC chain verifier accepts Python-written chains.
//
// Usage:
//
//	go run ./cmd/verify <data_dir> <world_name> [key]
//
// data_dir must contain {world_name}/universe.db.
package main

import (
	"fmt"
	"os"

	"github.com/elastik/go/core"
)

// The verify binary imports native's sqlite DB adapter indirectly by
// duplicating the minimum needed functions. We don't want to pull in
// the full HTTP server. So we link the same adapter package via a
// small shim file — see db_adapter.go sibling.

func main() {
	if len(os.Args) < 3 {
		fmt.Fprintln(os.Stderr, "usage: verify <data_dir> <world> [key]")
		os.Exit(2)
	}
	dataDir := os.Args[1]
	world := os.Args[2]
	key := []byte("elastik-dev-key")
	if len(os.Args) > 3 {
		key = []byte(os.Args[3])
	}

	db := newDB(dataDir)
	if err := core.VerifyChain(db, key, world); err != nil {
		fmt.Fprintf(os.Stderr, "FAIL: %v\n", err)
		os.Exit(1)
	}
	events, _ := db.ReadEvents(world)
	fmt.Printf("OK - verified %d events in %s/%s\n", len(events), dataDir, world)
}
