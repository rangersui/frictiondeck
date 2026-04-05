package core

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"
)

// ErrChainBroken is returned by VerifyChain when an event's stored
// hmac or prev_hmac link does not match what the algorithm would
// produce. The error message includes the offending event index so
// operators can jump straight to it.
type ErrChainBroken struct {
	World string
	Index int
	Field string // "hmac" or "prev_hmac"
}

func (e *ErrChainBroken) Error() string {
	return fmt.Sprintf("chain broken in world %q at event %d: %s mismatch", e.World, e.Index, e.Field)
}

// Now is the time source used for event timestamps. Tests override it
// to produce deterministic output. Matches Python's
// datetime('now') format: "YYYY-MM-DD HH:MM:SS" in UTC.
var Now = func() string {
	return time.Now().UTC().Format("2006-01-02 15:04:05")
}

// encodePayload serializes a value for the HMAC chain.
//
// Python server.py uses `json.dumps(payload or {}, ensure_ascii=False)`
// which means:
//   - nil / empty → "{}"
//   - default separators (", ", ": ") with spaces
//   - unicode kept raw
//
// Go's encoding/json uses minimal separators ("," and ":"). This means
// the Go HMAC chain is INTERNALLY consistent but NOT byte-identical to
// a chain produced by the Python server for the same payload. That's
// acceptable for v2.0 — each server signs its own chain. v3.0 (browser
// node) ships the same Go code on both sides so the chain is identical
// by construction.
//
// TODO(v2.1): add optional python-compat mode for cross-server audit.
func encodePayload(payload any) (string, error) {
	if payload == nil {
		return "{}", nil
	}
	b, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// LogEvent appends one HMAC-chained event for the given world. It
// reads the last hmac from the database, computes the next hash as
//
//	hex(hmac_sha256(key, prev_hmac || payload_json))
//
// and writes the new row. This matches server.py log_event byte-for-
// byte for the *linking* step: prev+payload is hashed under `key`
// using sha256 and lowercased to hex.
func LogEvent(db DB, key []byte, world, eventType string, payload any) error {
	p, err := encodePayload(payload)
	if err != nil {
		return err
	}
	prev, err := db.LastHMAC(world)
	if err != nil {
		return err
	}
	sum := computeHMAC(key, prev, p)
	return db.InsertEvent(world, Event{
		Timestamp: Now(),
		EventType: eventType,
		Payload:   p,
		HMAC:      sum,
		PrevHMAC:  prev,
	})
}

// computeHMAC is the single source of truth for the chain's hash
// step: hex(hmac_sha256(key, prev || payload)). Extracted so LogEvent
// and VerifyChain cannot drift from each other.
func computeHMAC(key []byte, prev, payload string) string {
	h := hmac.New(sha256.New, key)
	h.Write([]byte(prev))
	h.Write([]byte(payload))
	return hex.EncodeToString(h.Sum(nil))
}

// VerifyChain walks the event log of `world` oldest-to-newest and
// checks two invariants at every step:
//
//  1. event[i].prev_hmac == event[i-1].hmac   (link integrity)
//     (event[0].prev_hmac must be the empty string)
//  2. event[i].hmac       == hex(hmac_sha256(key, prev || payload))
//     (payload tamper detection)
//
// Returns nil if the chain is intact. Returns *ErrChainBroken with
// the offending index on the first mismatch. Returns the underlying
// error if the DB read fails.
//
// An empty log is considered valid. This is important: a freshly
// created world has no events, and verification should not spuriously
// fail on it.
func VerifyChain(db DB, key []byte, world string) error {
	events, err := db.ReadEvents(world)
	if err != nil {
		return err
	}
	prev := ""
	for i, e := range events {
		if e.PrevHMAC != prev {
			return &ErrChainBroken{World: world, Index: i, Field: "prev_hmac"}
		}
		if computeHMAC(key, prev, e.Payload) != e.HMAC {
			return &ErrChainBroken{World: world, Index: i, Field: "hmac"}
		}
		prev = e.HMAC
	}
	return nil
}
