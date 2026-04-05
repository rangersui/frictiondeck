package core

import (
	"encoding/json"
	"errors"
)

// Sentinel errors returned by core. The native HTTP layer maps each to
// a status code. The mapping table lives next to the router so core
// stays transport-agnostic.
var (
	ErrInvalidName = errors.New("invalid world name")
	ErrNotFound    = errors.New("world not found")
	ErrInvalidBody = errors.New("invalid body")
)

// ExtractBody mirrors server.py _extract: if the incoming body looks
// like a JSON object (and the action isn't "patch"), unwrap one of
// the known content fields; otherwise return as-is.
//
// Python's `or` chain skips empty strings, so we must too.
func ExtractBody(body, action string) string {
	if action == "patch" || body == "" || body[0] != '{' {
		return body
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(body), &m); err != nil {
		return body
	}
	for _, k := range []string{"body", "content", "text"} {
		if v, ok := m[k].(string); ok && v != "" {
			return v
		}
	}
	return body
}

// ReadWorld returns the current stage for `name`, or ErrNotFound if
// the world does not yet exist. Read must NOT create the world as a
// side effect — this matches server.py's 404 behavior and blocks
// probing attacks that try to mint worlds just by reading them.
func ReadWorld(db DB, name string) (Stage, error) {
	if !ValidName(name) {
		return Stage{}, ErrInvalidName
	}
	if !db.WorldExists(name) {
		return Stage{}, ErrNotFound
	}
	return db.ReadStage(name)
}

// WriteWorld replaces stage_html and logs a stage_written event.
// Returns the new version number.
func WriteWorld(db DB, key []byte, name, body string) (int, error) {
	if !ValidName(name) {
		return 0, ErrInvalidName
	}
	body = ExtractBody(body, "write")
	v, err := db.WriteStage(name, body)
	if err != nil {
		return 0, err
	}
	if err := LogEvent(db, key, name, "stage_written", map[string]any{"len": len(body)}); err != nil {
		return 0, err
	}
	return v, nil
}

// AppendWorld appends to stage_html and logs stage_appended.
func AppendWorld(db DB, key []byte, name, body string) (int, error) {
	if !ValidName(name) {
		return 0, ErrInvalidName
	}
	body = ExtractBody(body, "append")
	v, err := db.AppendStage(name, body)
	if err != nil {
		return 0, err
	}
	if err := LogEvent(db, key, name, "stage_appended", map[string]any{"len": len(body)}); err != nil {
		return 0, err
	}
	return v, nil
}

// SyncWorld replaces stage_html WITHOUT bumping version. Used by
// clients that want to push local edits back without triggering a
// downstream reload storm. No event is logged — matches server.py.
func SyncWorld(db DB, name, body string) error {
	if !ValidName(name) {
		return ErrInvalidName
	}
	body = ExtractBody(body, "sync")
	return db.SyncStage(name, body)
}

// SetPending writes pending_js — code the browser is expected to
// execute on the next poll.
func SetPending(db DB, name, body string) error {
	if !ValidName(name) {
		return ErrInvalidName
	}
	return db.SetPending(name, ExtractBody(body, "pending"))
}

// SetResult writes js_result — the browser's response for a pending
// exec request.
func SetResult(db DB, name, body string) error {
	if !ValidName(name) {
		return ErrInvalidName
	}
	return db.SetResult(name, ExtractBody(body, "result"))
}

// ClearWorld zeroes pending_js and js_result without touching
// stage_html.
func ClearWorld(db DB, name string) error {
	if !ValidName(name) {
		return ErrInvalidName
	}
	return db.ClearStage(name)
}

// ListStages is a thin pass-through; kept in core so the HTTP layer
// never touches DB directly even for simple reads.
func ListStages(db DB) ([]StageInfo, error) {
	return db.ListStages()
}
