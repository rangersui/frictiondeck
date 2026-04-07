package core

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
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

// Timestamps are written by the storage backend (native SQLite uses
// SQL `datetime('now')`) so core does not carry a time source. This
// matches server.py and keeps `core` free of wall-clock dependencies.
// Event.Timestamp is populated on the read path only.

// encodePayload serializes a value for the HMAC chain, byte-identical
// to Python's `json.dumps(payload or {}, ensure_ascii=False)`.
//
// Python defaults:
//   - separators: ", " and ": " (with spaces)
//   - ensure_ascii=False — raw UTF-8 for non-ASCII
//   - HTML chars not escaped
//   - dict insertion order preserved (Python 3.7+)
//
// Go's encoding/json defaults differ on three axes:
//  1. minimal separators "," and ":" — we post-process to add spaces
//  2. HTML-escapes <, >, & to \u003c etc. — we disable via SetEscapeHTML
//  3. alphabetizes map keys — unavoidable without a custom encoder,
//     but the only live LogEvent payloads are single-key ({"len": N}),
//     so this doesn't bite in v2.0. Multi-key payloads added later
//     need an ordered encoder.
//
// The result is that a Go-produced event log can be verified under
// Python's HMAC rules and vice versa, for all payload shapes currently
// in use.
func encodePayload(payload any) (string, error) {
	if payload == nil {
		return "{}", nil
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(payload); err != nil {
		return "", err
	}
	// json.Encoder appends a trailing newline; strip it.
	raw := strings.TrimRight(buf.String(), "\n")
	return pythonizeJSON(raw), nil
}

// pythonizeJSON rewrites minimal-separator JSON to match Python's
// default `json.dumps` output: `,` → `, ` and `:` → `: `, but only
// when those characters are OUTSIDE a string literal. Backslash
// escapes inside strings are honored so a `\"` doesn't end the string
// prematurely.
//
// This is a byte-level pass, not a re-parse, so it preserves every
// other formatting detail Go's encoder produced (key order, numeric
// representation, unicode escapes for control chars, etc.) exactly.
func pythonizeJSON(s string) string {
	var b strings.Builder
	b.Grow(len(s) + len(s)/8)
	inString := false
	escape := false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if escape {
			b.WriteByte(c)
			escape = false
			continue
		}
		if inString {
			if c == '\\' {
				b.WriteByte(c)
				escape = true
				continue
			}
			if c == '"' {
				inString = false
			}
			b.WriteByte(c)
			continue
		}
		switch c {
		case '"':
			inString = true
			b.WriteByte(c)
		case ',':
			b.WriteString(", ")
		case ':':
			b.WriteString(": ")
		default:
			b.WriteByte(c)
		}
	}
	return b.String()
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
	// Timestamp is filled in by the storage backend at insert time;
	// core does not set it here (see note above).
	return db.InsertEvent(world, Event{
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
