package core

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"sort"
	"testing"
)

// ─── mock DB ─────────────────────────────────────────────────────────
//
// mockDB implements the DB interface entirely in memory. It exists
// only to let core_test.go exercise pure logic without dragging in
// SQLite. Every native feature (WAL, concurrency, schema DDL) lives
// in native/db_sqlite.go — this file stays tiny on purpose.

type mockWorld struct {
	stage  Stage
	events []Event
}

type mockDB struct {
	worlds map[string]*mockWorld
	// failAt lets tests inject errors at a named method to verify
	// error propagation without threading a counter through core.
	failAt string
}

func newMock() *mockDB { return &mockDB{worlds: map[string]*mockWorld{}} }

func (m *mockDB) get(name string, create bool) *mockWorld {
	w, ok := m.worlds[name]
	if !ok && create {
		w = &mockWorld{}
		m.worlds[name] = w
	}
	return w
}

func (m *mockDB) WorldExists(name string) bool {
	_, ok := m.worlds[name]
	return ok
}

func (m *mockDB) ReadStage(name string) (Stage, error) {
	if m.failAt == "ReadStage" {
		return Stage{}, errors.New("boom")
	}
	w := m.get(name, false)
	if w == nil {
		return Stage{}, ErrNotFound
	}
	return w.stage, nil
}

func (m *mockDB) WriteStage(name, html string) (int, error) {
	if m.failAt == "WriteStage" {
		return 0, errors.New("boom")
	}
	w := m.get(name, true)
	w.stage.StageHTML = html
	w.stage.Version++
	return w.stage.Version, nil
}

func (m *mockDB) AppendStage(name, html string) (int, error) {
	if m.failAt == "AppendStage" {
		return 0, errors.New("boom")
	}
	w := m.get(name, true)
	w.stage.StageHTML += html
	w.stage.Version++
	return w.stage.Version, nil
}

func (m *mockDB) SyncStage(name, html string) error {
	if m.failAt == "SyncStage" {
		return errors.New("boom")
	}
	w := m.get(name, true)
	w.stage.StageHTML = html
	return nil
}

func (m *mockDB) SetPending(name, js string) error {
	if m.failAt == "SetPending" {
		return errors.New("boom")
	}
	w := m.get(name, true)
	w.stage.PendingJS = js
	return nil
}

func (m *mockDB) SetResult(name, js string) error {
	if m.failAt == "SetResult" {
		return errors.New("boom")
	}
	w := m.get(name, true)
	w.stage.JSResult = js
	return nil
}

func (m *mockDB) ClearStage(name string) error {
	if m.failAt == "ClearStage" {
		return errors.New("boom")
	}
	w := m.get(name, true)
	w.stage.PendingJS = ""
	w.stage.JSResult = ""
	return nil
}

func (m *mockDB) LastHMAC(name string) (string, error) {
	if m.failAt == "LastHMAC" {
		return "", errors.New("boom")
	}
	w := m.get(name, false)
	if w == nil || len(w.events) == 0 {
		return "", nil
	}
	return w.events[len(w.events)-1].HMAC, nil
}

func (m *mockDB) InsertEvent(name string, e Event) error {
	if m.failAt == "InsertEvent" {
		return errors.New("boom")
	}
	w := m.get(name, true)
	w.events = append(w.events, e)
	return nil
}

func (m *mockDB) ReadEvents(name string) ([]Event, error) {
	if m.failAt == "ReadEvents" {
		return nil, errors.New("boom")
	}
	w := m.get(name, false)
	if w == nil {
		return nil, nil
	}
	out := make([]Event, len(w.events))
	copy(out, w.events)
	return out, nil
}

func (m *mockDB) ListStages() ([]StageInfo, error) {
	if m.failAt == "ListStages" {
		return nil, errors.New("boom")
	}
	out := make([]StageInfo, 0, len(m.worlds))
	for name, w := range m.worlds {
		out = append(out, StageInfo{Name: name, Version: w.stage.Version, UpdatedAt: ""})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// ─── tests ────────────────────────────────────────────────────────────

func TestValidName(t *testing.T) {
	good := []string{"a", "A1", "world-1", "my_world", "default", "0name"}
	bad := []string{"", "-leading", "_under", "has space", "path/trav", "..", ".hidden", "a/b"}
	for _, s := range good {
		if !ValidName(s) {
			t.Errorf("ValidName(%q) = false, want true", s)
		}
	}
	for _, s := range bad {
		if ValidName(s) {
			t.Errorf("ValidName(%q) = true, want false", s)
		}
	}
}

func TestExtractBody(t *testing.T) {
	cases := []struct {
		body, action, want string
	}{
		// plain text — pass through
		{"hello world", "write", "hello world"},
		// empty → empty
		{"", "write", ""},
		// not an object — pass through
		{"[1,2,3]", "write", "[1,2,3]"},
		// patch action preserves body even when JSON
		{`{"body":"x"}`, "patch", `{"body":"x"}`},
		// body/content/text unwrapping
		{`{"body":"hi"}`, "write", "hi"},
		{`{"content":"hi"}`, "write", "hi"},
		{`{"text":"hi"}`, "write", "hi"},
		// empty string in body is falsy → fall through
		{`{"body":"","content":"yes"}`, "write", "yes"},
		// no known key → return original
		{`{"other":"x"}`, "write", `{"other":"x"}`},
		// malformed JSON — return original
		{`{not json`, "write", `{not json`},
	}
	for _, c := range cases {
		got := ExtractBody(c.body, c.action)
		if got != c.want {
			t.Errorf("ExtractBody(%q,%q)=%q want %q", c.body, c.action, got, c.want)
		}
	}
}

func TestWriteAndReadWorld(t *testing.T) {
	db := newMock()
	key := []byte("test-key")

	// Read before any write → 404.
	if _, err := ReadWorld(db, "w1"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("expected ErrNotFound, got %v", err)
	}

	v, err := WriteWorld(db, key, "w1", "hello")
	if err != nil {
		t.Fatal(err)
	}
	if v != 1 {
		t.Errorf("version=%d want 1", v)
	}

	s, err := ReadWorld(db, "w1")
	if err != nil {
		t.Fatal(err)
	}
	if s.StageHTML != "hello" || s.Version != 1 {
		t.Errorf("read got %+v", s)
	}

	// JSON body unwrapping should apply to writes too.
	if _, err := WriteWorld(db, key, "w1", `{"body":"unwrapped"}`); err != nil {
		t.Fatal(err)
	}
	s, _ = ReadWorld(db, "w1")
	if s.StageHTML != "unwrapped" {
		t.Errorf("expected unwrapped, got %q", s.StageHTML)
	}
	if s.Version != 2 {
		t.Errorf("version=%d want 2", s.Version)
	}

	// Invalid name rejected at every entry point.
	if _, err := WriteWorld(db, key, "bad name", "x"); !errors.Is(err, ErrInvalidName) {
		t.Errorf("want invalid name, got %v", err)
	}
	if _, err := ReadWorld(db, "bad name"); !errors.Is(err, ErrInvalidName) {
		t.Errorf("want invalid name, got %v", err)
	}
}

func TestAppendBumpsVersion(t *testing.T) {
	db := newMock()
	key := []byte("k")
	WriteWorld(db, key, "w", "hello ")
	v, err := AppendWorld(db, key, "w", "world")
	if err != nil {
		t.Fatal(err)
	}
	if v != 2 {
		t.Errorf("version=%d want 2", v)
	}
	s, _ := ReadWorld(db, "w")
	if s.StageHTML != "hello world" {
		t.Errorf("got %q", s.StageHTML)
	}

	if _, err := AppendWorld(db, key, "bad name", ""); !errors.Is(err, ErrInvalidName) {
		t.Error("append should validate name")
	}
}

func TestSyncDoesNotBumpVersion(t *testing.T) {
	db := newMock()
	key := []byte("k")
	WriteWorld(db, key, "w", "v1 content")
	before, _ := ReadWorld(db, "w")

	if err := SyncWorld(db, "w", "v2 content"); err != nil {
		t.Fatal(err)
	}
	after, _ := ReadWorld(db, "w")
	if after.Version != before.Version {
		t.Errorf("sync bumped version %d → %d", before.Version, after.Version)
	}
	if after.StageHTML != "v2 content" {
		t.Errorf("sync did not update html: %q", after.StageHTML)
	}

	if err := SyncWorld(db, "bad name", ""); !errors.Is(err, ErrInvalidName) {
		t.Error("sync should validate name")
	}
}

func TestPendingResultClear(t *testing.T) {
	db := newMock()

	if err := SetPending(db, "w", "console.log(1)"); err != nil {
		t.Fatal(err)
	}
	if err := SetResult(db, "w", "1"); err != nil {
		t.Fatal(err)
	}
	s, _ := db.ReadStage("w")
	if s.PendingJS != "console.log(1)" || s.JSResult != "1" {
		t.Errorf("pending/result not set: %+v", s)
	}

	if err := ClearWorld(db, "w"); err != nil {
		t.Fatal(err)
	}
	s, _ = db.ReadStage("w")
	if s.PendingJS != "" || s.JSResult != "" {
		t.Errorf("clear didn't zero fields: %+v", s)
	}

	for _, fn := range []func(string) error{
		func(n string) error { return SetPending(db, n, "") },
		func(n string) error { return SetResult(db, n, "") },
		func(n string) error { return ClearWorld(db, n) },
	} {
		if err := fn("bad name"); !errors.Is(err, ErrInvalidName) {
			t.Error("want invalid name")
		}
	}
}

func TestHMACChain(t *testing.T) {
	db := newMock()
	key := []byte("elastik-test-key")

	if _, err := WriteWorld(db, key, "w", "a"); err != nil {
		t.Fatal(err)
	}
	if _, err := WriteWorld(db, key, "w", "b"); err != nil {
		t.Fatal(err)
	}
	if _, err := AppendWorld(db, key, "w", "c"); err != nil {
		t.Fatal(err)
	}

	w := db.get("w", false)
	if len(w.events) != 3 {
		t.Fatalf("expected 3 events, got %d", len(w.events))
	}

	// First event must link to "" (empty prev).
	if w.events[0].PrevHMAC != "" {
		t.Errorf("first event prev should be empty, got %q", w.events[0].PrevHMAC)
	}
	// Subsequent events link to their predecessor.
	for i := 1; i < len(w.events); i++ {
		if w.events[i].PrevHMAC != w.events[i-1].HMAC {
			t.Errorf("event %d prev does not match previous hmac", i)
		}
	}

	// Verify each hmac by recomputing independently.
	for i, e := range w.events {
		h := hmac.New(sha256.New, key)
		h.Write([]byte(e.PrevHMAC))
		h.Write([]byte(e.Payload))
		want := hex.EncodeToString(h.Sum(nil))
		if e.HMAC != want {
			t.Errorf("event %d hmac mismatch: got %s want %s", i, e.HMAC, want)
		}
	}

	// Event types and payloads
	wantTypes := []string{"stage_written", "stage_written", "stage_appended"}
	for i, et := range wantTypes {
		if w.events[i].EventType != et {
			t.Errorf("event %d type=%q want %q", i, w.events[i].EventType, et)
		}
	}
	if w.events[0].Payload != `{"len": 1}` {
		t.Errorf("payload = %q", w.events[0].Payload)
	}
}

// TestPayloadLenIsCodepoints pins the cross-language compatibility
// rule: server.py logs {"len": len(body)} where len() counts
// codepoints, not bytes. Go must do the same or the HMAC chain
// diverges for any non-ASCII input.
//
// If this test fails because someone switched back to len(body),
// they've just broken every Chinese/Japanese/emoji-containing event
// log. Do not "fix" by updating the expected value.
func TestPayloadLenIsCodepoints(t *testing.T) {
	db := newMock()
	key := []byte("k")

	// "中文" — 2 codepoints, 6 UTF-8 bytes.
	if _, err := WriteWorld(db, key, "w", "中文"); err != nil {
		t.Fatal(err)
	}
	// "café" — 4 codepoints, 5 UTF-8 bytes.
	if _, err := AppendWorld(db, key, "w", "café"); err != nil {
		t.Fatal(err)
	}
	// Mixed emoji — "a🙂b" — 3 codepoints, 6 bytes.
	if _, err := AppendWorld(db, key, "w", "a🙂b"); err != nil {
		t.Fatal(err)
	}

	events := db.worlds["w"].events
	wantPayloads := []string{
		`{"len": 2}`, // 中文
		`{"len": 4}`, // café
		`{"len": 3}`, // a🙂b
	}
	for i, want := range wantPayloads {
		if events[i].Payload != want {
			t.Errorf("event %d payload = %q, want %q (Python len() counts codepoints)",
				i, events[i].Payload, want)
		}
	}
}

// TestEncodePayloadPythonCompat pins the Python-compat format.
// Every case here should match json.dumps(x, ensure_ascii=False)
// byte-for-byte in Python 3.
func TestEncodePayloadPythonCompat(t *testing.T) {
	cases := []struct {
		name    string
		payload any
		want    string
	}{
		{"nil", nil, "{}"},
		{"single int", map[string]any{"len": 5}, `{"len": 5}`},
		{"single string", map[string]any{"kind": "write"}, `{"kind": "write"}`},
		{"unicode value", map[string]any{"v": "中文"}, `{"v": "中文"}`},
		{"colon inside string", map[string]any{"k": "a:b"}, `{"k": "a:b"}`},
		{"comma inside string", map[string]any{"k": "a,b"}, `{"k": "a,b"}`},
		{"escaped quote", map[string]any{"k": `a"b`}, `{"k": "a\"b"}`},
		{"html chars not escaped", map[string]any{"k": "<x>&"}, `{"k": "<x>&"}`},
	}
	for _, c := range cases {
		got, err := encodePayload(c.payload)
		if err != nil {
			t.Errorf("%s: %v", c.name, err)
			continue
		}
		if got != c.want {
			t.Errorf("%s: got %q want %q", c.name, got, c.want)
		}
	}
}

func TestVerifyChainHappyPath(t *testing.T) {
	db := newMock()
	key := []byte("verify-test-key")

	// Empty log is valid.
	if err := VerifyChain(db, key, "w"); err != nil {
		t.Errorf("empty chain should verify, got %v", err)
	}

	// Write a few events, verify the chain.
	WriteWorld(db, key, "w", "one")
	AppendWorld(db, key, "w", "two")
	WriteWorld(db, key, "w", "three")
	if err := VerifyChain(db, key, "w"); err != nil {
		t.Errorf("honest chain should verify, got %v", err)
	}

	// Wrong key must fail.
	if err := VerifyChain(db, []byte("wrong-key"), "w"); err == nil {
		t.Error("chain should NOT verify under wrong key")
	}
}

func TestVerifyChainDetectsTamper(t *testing.T) {
	key := []byte("k")

	// ─ Tamper the payload of event 1 ────────────────────────
	db1 := newMock()
	WriteWorld(db1, key, "w", "original")
	WriteWorld(db1, key, "w", "second")
	db1.worlds["w"].events[1].Payload = `{"len":999}` // lie about the length
	err := VerifyChain(db1, key, "w")
	var cb *ErrChainBroken
	if !errors.As(err, &cb) || cb.Index != 1 || cb.Field != "hmac" {
		t.Errorf("payload tamper: expected ErrChainBroken{Index:1, Field:hmac}, got %v", err)
	}

	// ─ Tamper the stored hmac of event 0 ────────────────────
	db2 := newMock()
	WriteWorld(db2, key, "w", "one")
	WriteWorld(db2, key, "w", "two")
	db2.worlds["w"].events[0].HMAC = "0000000000000000000000000000000000000000000000000000000000000000"
	err = VerifyChain(db2, key, "w")
	if !errors.As(err, &cb) || cb.Index != 0 || cb.Field != "hmac" {
		t.Errorf("hmac tamper: expected ErrChainBroken{Index:0, Field:hmac}, got %v", err)
	}

	// ─ Break the prev link on event 1 ────────────────────────
	db3 := newMock()
	WriteWorld(db3, key, "w", "a")
	WriteWorld(db3, key, "w", "b")
	db3.worlds["w"].events[1].PrevHMAC = "deadbeef"
	err = VerifyChain(db3, key, "w")
	if !errors.As(err, &cb) || cb.Index != 1 || cb.Field != "prev_hmac" {
		t.Errorf("prev link tamper: expected ErrChainBroken{Index:1, Field:prev_hmac}, got %v", err)
	}

	// ─ First event's prev must be empty string ───────────────
	db4 := newMock()
	WriteWorld(db4, key, "w", "only")
	db4.worlds["w"].events[0].PrevHMAC = "somethingnonempty"
	err = VerifyChain(db4, key, "w")
	if !errors.As(err, &cb) || cb.Index != 0 || cb.Field != "prev_hmac" {
		t.Errorf("nonempty first prev: expected ErrChainBroken{Index:0, Field:prev_hmac}, got %v", err)
	}
}

func TestVerifyChainReadError(t *testing.T) {
	db := newMock()
	db.failAt = "ReadEvents"
	if err := VerifyChain(db, []byte("k"), "w"); err == nil {
		t.Error("expected ReadEvents error to propagate")
	}
}

func TestLogEventEmptyPayload(t *testing.T) {
	db := newMock()
	// nil payload → "{}"
	if err := LogEvent(db, []byte("k"), "w", "ping", nil); err != nil {
		t.Fatal(err)
	}
	w := db.get("w", false)
	if w.events[0].Payload != "{}" {
		t.Errorf("nil payload = %q want {}", w.events[0].Payload)
	}
}

func TestListStages(t *testing.T) {
	db := newMock()
	key := []byte("k")
	WriteWorld(db, key, "alpha", "1")
	WriteWorld(db, key, "beta", "1")
	WriteWorld(db, key, "alpha", "2") // two writes → version 2

	list, err := ListStages(db)
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 {
		t.Fatalf("want 2 stages, got %d", len(list))
	}
	if list[0].Name != "alpha" || list[0].Version != 2 {
		t.Errorf("alpha: %+v", list[0])
	}
	if list[1].Name != "beta" || list[1].Version != 1 {
		t.Errorf("beta: %+v", list[1])
	}
}

func TestErrorPropagation(t *testing.T) {
	key := []byte("k")

	// Each DB-side failure should bubble up.
	cases := []struct {
		failAt string
		fn     func(DB) error
	}{
		{"WriteStage", func(db DB) error { _, e := WriteWorld(db, key, "w", "x"); return e }},
		{"AppendStage", func(db DB) error { _, e := AppendWorld(db, key, "w", "x"); return e }},
		{"SyncStage", func(db DB) error { return SyncWorld(db, "w", "x") }},
		{"SetPending", func(db DB) error { return SetPending(db, "w", "x") }},
		{"SetResult", func(db DB) error { return SetResult(db, "w", "x") }},
		{"ClearStage", func(db DB) error { return ClearWorld(db, "w") }},
		{"ListStages", func(db DB) error { _, e := ListStages(db); return e }},
		{"LastHMAC", func(db DB) error { return LogEvent(db, key, "w", "t", nil) }},
		{"InsertEvent", func(db DB) error { return LogEvent(db, key, "w", "t", nil) }},
	}
	for _, c := range cases {
		db := newMock()
		db.failAt = c.failAt
		if err := c.fn(db); err == nil {
			t.Errorf("%s: expected error, got nil", c.failAt)
		}
	}

	// ReadStage failure path: create the world so WorldExists passes.
	db := newMock()
	db.worlds["w"] = &mockWorld{}
	db.failAt = "ReadStage"
	if _, err := ReadWorld(db, "w"); err == nil {
		t.Error("ReadStage: expected error")
	}

	// Write/Append should also propagate a LogEvent (InsertEvent) failure
	// that happens after the stage mutation succeeds.
	db2 := newMock()
	db2.worlds["w"] = &mockWorld{} // pre-create so WriteStage works, then fail on InsertEvent
	db2.failAt = "InsertEvent"
	if _, err := WriteWorld(db2, key, "w", "x"); err == nil {
		t.Error("WriteWorld: expected LogEvent error to propagate")
	}
	db3 := newMock()
	db3.worlds["w"] = &mockWorld{}
	db3.failAt = "InsertEvent"
	if _, err := AppendWorld(db3, key, "w", "x"); err == nil {
		t.Error("AppendWorld: expected LogEvent error to propagate")
	}
}
