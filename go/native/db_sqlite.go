package main

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"github.com/elastik/go/core"

	_ "modernc.org/sqlite"
)

// sqliteDB implements core.DB backed by one SQLite file per world.
// The schema mirrors server.py byte-for-byte so the Python and Go
// servers can read each other's data/{name}/universe.db files.
type sqliteDB struct {
	root string

	mu    sync.Mutex
	conns map[string]*sql.DB
}

const schema = `
CREATE TABLE IF NOT EXISTS stage_meta(
    id INTEGER PRIMARY KEY CHECK(id=1),
    stage_html TEXT DEFAULT '',
    pending_js TEXT DEFAULT '',
    js_result TEXT DEFAULT '',
    version INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT ''
);
INSERT OR IGNORE INTO stage_meta(id, updated_at) VALUES(1, datetime('now'));
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    hmac TEXT NOT NULL,
    prev_hmac TEXT DEFAULT ''
);
`

func newSQLiteDB(root string) *sqliteDB {
	return &sqliteDB{root: root, conns: map[string]*sql.DB{}}
}

func (s *sqliteDB) dir(name string) string { return filepath.Join(s.root, name) }
func (s *sqliteDB) file(name string) string {
	return filepath.Join(s.dir(name), "universe.db")
}

// conn returns a cached *sql.DB for the given world, creating the
// directory, the database file and the schema on first use.
func (s *sqliteDB) conn(name string) (*sql.DB, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if c, ok := s.conns[name]; ok {
		return c, nil
	}
	if err := os.MkdirAll(s.dir(name), 0o755); err != nil {
		return nil, err
	}
	c, err := sql.Open("sqlite", s.file(name))
	if err != nil {
		return nil, err
	}
	// modernc.org/sqlite is a pure-Go driver. A single connection is
	// fine for our usage (single-writer, no goroutine fan-out).
	c.SetMaxOpenConns(1)
	if _, err := c.Exec("PRAGMA journal_mode=WAL"); err != nil {
		return nil, err
	}
	if _, err := c.Exec("PRAGMA synchronous=FULL"); err != nil {
		return nil, err
	}
	if _, err := c.Exec(schema); err != nil {
		return nil, fmt.Errorf("init schema: %w", err)
	}
	s.conns[name] = c
	return c, nil
}

func (s *sqliteDB) WorldExists(name string) bool {
	_, err := os.Stat(s.file(name))
	return err == nil
}

func (s *sqliteDB) ReadStage(name string) (core.Stage, error) {
	c, err := s.conn(name)
	if err != nil {
		return core.Stage{}, err
	}
	var st core.Stage
	err = c.QueryRow("SELECT stage_html, pending_js, js_result, version FROM stage_meta WHERE id=1").
		Scan(&st.StageHTML, &st.PendingJS, &st.JSResult, &st.Version)
	return st, err
}

func (s *sqliteDB) WriteStage(name, html string) (int, error) {
	c, err := s.conn(name)
	if err != nil {
		return 0, err
	}
	if _, err := c.Exec(
		"UPDATE stage_meta SET stage_html=?, version=version+1, updated_at=datetime('now') WHERE id=1",
		html,
	); err != nil {
		return 0, err
	}
	return s.version(c)
}

func (s *sqliteDB) AppendStage(name, html string) (int, error) {
	c, err := s.conn(name)
	if err != nil {
		return 0, err
	}
	if _, err := c.Exec(
		"UPDATE stage_meta SET stage_html=stage_html||?, version=version+1, updated_at=datetime('now') WHERE id=1",
		html,
	); err != nil {
		return 0, err
	}
	return s.version(c)
}

func (s *sqliteDB) version(c *sql.DB) (int, error) {
	var v int
	err := c.QueryRow("SELECT version FROM stage_meta WHERE id=1").Scan(&v)
	return v, err
}

func (s *sqliteDB) SyncStage(name, html string) error {
	c, err := s.conn(name)
	if err != nil {
		return err
	}
	_, err = c.Exec(
		"UPDATE stage_meta SET stage_html=?, updated_at=datetime('now') WHERE id=1",
		html,
	)
	return err
}

func (s *sqliteDB) SetPending(name, js string) error {
	c, err := s.conn(name)
	if err != nil {
		return err
	}
	_, err = c.Exec(
		"UPDATE stage_meta SET pending_js=?, updated_at=datetime('now') WHERE id=1",
		js,
	)
	return err
}

func (s *sqliteDB) SetResult(name, js string) error {
	c, err := s.conn(name)
	if err != nil {
		return err
	}
	_, err = c.Exec(
		"UPDATE stage_meta SET js_result=?, updated_at=datetime('now') WHERE id=1",
		js,
	)
	return err
}

func (s *sqliteDB) ClearStage(name string) error {
	c, err := s.conn(name)
	if err != nil {
		return err
	}
	_, err = c.Exec(
		"UPDATE stage_meta SET pending_js='', js_result='', updated_at=datetime('now') WHERE id=1",
	)
	return err
}

func (s *sqliteDB) LastHMAC(name string) (string, error) {
	c, err := s.conn(name)
	if err != nil {
		return "", err
	}
	var h string
	err = c.QueryRow("SELECT hmac FROM events ORDER BY id DESC LIMIT 1").Scan(&h)
	if err == sql.ErrNoRows {
		return "", nil
	}
	return h, err
}

func (s *sqliteDB) InsertEvent(name string, e core.Event) error {
	c, err := s.conn(name)
	if err != nil {
		return err
	}
	_, err = c.Exec(
		"INSERT INTO events(timestamp, event_type, payload, hmac, prev_hmac) VALUES(datetime('now'), ?, ?, ?, ?)",
		e.EventType, e.Payload, e.HMAC, e.PrevHMAC,
	)
	return err
}

func (s *sqliteDB) ReadEvents(name string) ([]core.Event, error) {
	c, err := s.conn(name)
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

func (s *sqliteDB) ListStages() ([]core.StageInfo, error) {
	// Scan the data directory for every subdir containing universe.db.
	entries, err := os.ReadDir(s.root)
	if err != nil {
		if os.IsNotExist(err) {
			return []core.StageInfo{}, nil
		}
		return nil, err
	}
	var out []core.StageInfo
	for _, ent := range entries {
		if !ent.IsDir() {
			continue
		}
		if _, err := os.Stat(filepath.Join(s.root, ent.Name(), "universe.db")); err != nil {
			continue
		}
		c, err := s.conn(ent.Name())
		if err != nil {
			continue
		}
		var info core.StageInfo
		info.Name = ent.Name()
		if err := c.QueryRow("SELECT version, updated_at FROM stage_meta WHERE id=1").
			Scan(&info.Version, &info.UpdatedAt); err != nil {
			continue
		}
		out = append(out, info)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}
