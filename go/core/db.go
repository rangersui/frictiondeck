package core

// DB is the storage contract. Two implementations exist:
//
//   - native/db_sqlite.go   — modernc.org/sqlite, files on disk
//   - wasm/db_opfs.go       — SQLite WASM on OPFS (coming in v3.0)
//
// Every method takes a world name. Implementations are responsible for
// opening/creating the underlying universe.db on first access and
// caching the handle. Core never touches files or SQL.
//
// All methods must be safe to call from a single goroutine; the native
// HTTP server serializes per-world access upstream if concurrency is
// needed. Core itself makes no goroutine assumptions.
type DB interface {
	// WorldExists reports whether the backing store for `name` already
	// has a universe. Used to implement server.py's read-returns-404
	// behavior without creating the world as a side effect.
	WorldExists(name string) bool

	// ReadStage returns the current stage_meta row. It is only called
	// after WorldExists returned true.
	ReadStage(name string) (Stage, error)

	// WriteStage replaces stage_html, bumps version, touches updated_at.
	// Returns the new version.
	WriteStage(name, html string) (version int, err error)

	// AppendStage concatenates to stage_html, bumps version, touches
	// updated_at. Returns the new version.
	AppendStage(name, html string) (version int, err error)

	// SyncStage replaces stage_html without bumping version.
	SyncStage(name, html string) error

	// SetPending writes pending_js.
	SetPending(name, js string) error

	// SetResult writes js_result.
	SetResult(name, js string) error

	// ClearStage zeroes pending_js and js_result. stage_html is kept.
	ClearStage(name string) error

	// LastHMAC returns the most recent event's hmac, or "" if the log
	// is empty. Used as the "prev" link for the next event.
	LastHMAC(name string) (string, error)

	// InsertEvent appends one row to the events table.
	InsertEvent(name string, e Event) error

	// ReadEvents returns every event for a world, oldest first. Used
	// by VerifyChain and any audit tooling. For worlds with very
	// large histories a streaming variant may be added later; for
	// now full load is fine because typical worlds have O(100)
	// events and each row is small.
	ReadEvents(name string) ([]Event, error)

	// ListStages returns summaries of every world, sorted by name.
	ListStages() ([]StageInfo, error)
}
