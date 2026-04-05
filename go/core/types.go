// Package core — pure elastik protocol logic. No net/http. No database/sql.
// Same code compiles for native (go/native) and browser wasm (go/wasm, v3.0).
package core

import "regexp"

// Stage is the current visible state of a world, matching Python's
// stage_meta row shape. Field tags mirror server.py read output.
type Stage struct {
	StageHTML string `json:"stage_html"`
	PendingJS string `json:"pending_js"`
	JSResult  string `json:"js_result"`
	Version   int    `json:"version"`
}

// StageInfo is the summary returned by /stages.
type StageInfo struct {
	Name      string `json:"name"`
	Version   int    `json:"version"`
	UpdatedAt string `json:"updated_at"`
}

// Event is one row of the HMAC-chained append-only log.
type Event struct {
	Timestamp string
	EventType string
	Payload   string // JSON-encoded
	HMAC      string
	PrevHMAC  string
}

// validName mirrors server.py's _VALID_NAME exactly.
var validName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]*$`)

// ValidName reports whether s is a legal world name.
func ValidName(s string) bool { return validName.MatchString(s) }
