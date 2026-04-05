package main

// Minimal DB adapter for the verify CLI. VerifyChain only touches
// ReadEvents, so every other DB method is implemented as a panic —
// if someone extends VerifyChain and accidentally calls e.g.
// WriteStage here, the panic makes the mistake obvious immediately.
//
// The production adapter lives in go/native/db_sqlite.go. We do not
// import it because it's in package main; promoting it to a library
// package is on the v0.2 roadmap.

import (
	"database/sql"
	"fmt"
	"path/filepath"

	"github.com/elastik/go/core"
	_ "modernc.org/sqlite"
)

type verifyDB struct {
	dataDir string
	conns   map[string]*sql.DB
}

func newDB(dataDir string) *verifyDB {
	return &verifyDB{dataDir: dataDir, conns: map[string]*sql.DB{}}
}

func (v *verifyDB) conn(world string) (*sql.DB, error) {
	if c, ok := v.conns[world]; ok {
		return c, nil
	}
	p := filepath.Join(v.dataDir, world, "universe.db")
	c, err := sql.Open("sqlite", p)
	if err != nil {
		return nil, err
	}
	v.conns[world] = c
	return c, nil
}

func (v *verifyDB) ReadEvents(world string) ([]core.Event, error) {
	c, err := v.conn(world)
	if err != nil {
		return nil, err
	}
	rows, err := c.Query("SELECT timestamp, event_type, payload, hmac, prev_hmac FROM events ORDER BY id ASC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []core.Event
	for rows.Next() {
		var e core.Event
		if err := rows.Scan(&e.Timestamp, &e.EventType, &e.Payload, &e.HMAC, &e.PrevHMAC); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// ─── the rest are unused by VerifyChain ──────────────────────────────

func (v *verifyDB) WorldExists(string) bool { panic("verify: WorldExists not implemented") }
func (v *verifyDB) ReadStage(string) (core.Stage, error) {
	panic("verify: ReadStage not implemented")
}
func (v *verifyDB) WriteStage(string, string) (int, error) {
	panic(fmt.Errorf("verify: WriteStage not implemented"))
}
func (v *verifyDB) AppendStage(string, string) (int, error) {
	panic("verify: AppendStage not implemented")
}
func (v *verifyDB) SyncStage(string, string) error  { panic("verify: SyncStage not implemented") }
func (v *verifyDB) SetPending(string, string) error { panic("verify: SetPending not implemented") }
func (v *verifyDB) SetResult(string, string) error  { panic("verify: SetResult not implemented") }
func (v *verifyDB) ClearStage(string) error         { panic("verify: ClearStage not implemented") }
func (v *verifyDB) LastHMAC(string) (string, error) { panic("verify: LastHMAC not implemented") }
func (v *verifyDB) InsertEvent(string, core.Event) error {
	panic("verify: InsertEvent not implemented")
}
func (v *verifyDB) ListStages() ([]core.StageInfo, error) {
	panic("verify: ListStages not implemented")
}
