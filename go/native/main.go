// elastik-lite — Go native server.
//
// One binary. No Python. Same universe.db schema and HMAC chain as
// server.py so Python and Go servers can read each other's data.
//
// Scope: the protocol only. The plugin system (admin, auth, info,
// etc.) stays in Python for now. Go handles the core /v1/.../{action}
// routes plus /stages and static assets.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"

	"github.com/elastik/go/core"
)

// ─── config ──────────────────────────────────────────────────────────

type config struct {
	host    string
	port    string
	dataDir string
	key     []byte
	token   string
}

func loadConfig() config {
	c := config{
		host:    env("ELASTIK_HOST", "127.0.0.1"),
		port:    env("ELASTIK_PORT", "3005"),
		dataDir: env("ELASTIK_DATA", "data"),
		key:     []byte(env("ELASTIK_KEY", "elastik-dev-key")),
		token:   os.Getenv("ELASTIK_TOKEN"),
	}
	return c
}

func env(k, fallback string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return fallback
}

// ─── server state ────────────────────────────────────────────────────

type server struct {
	cfg    config
	db     *sqliteDB
	static staticFiles
}

const maxBody = 5 * 1024 * 1024

// ─── helpers ─────────────────────────────────────────────────────────

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// readBody enforces MAX_BODY and returns the raw bytes.
func readBody(r *http.Request) (string, error) {
	r.Body = http.MaxBytesReader(nil, r.Body, maxBody)
	b, err := io.ReadAll(r.Body)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// mapError converts a core sentinel error to an (HTTP status, message).
func mapError(err error) (int, string) {
	switch {
	case errors.Is(err, core.ErrInvalidName):
		return 400, "invalid world name"
	case errors.Is(err, core.ErrNotFound):
		return 404, "world not found"
	default:
		return 500, err.Error()
	}
}

// ─── path guards ─────────────────────────────────────────────────────
//
// NOTE on auth: server.py's *protocol* layer does NOT enforce
// ELASTIK_TOKEN on requests. Auth is handled by the auth plugin which
// sets server.py's `_auth` middleware. Since the plugin system stays
// in Python for v2.0 (see file header), Go Lite mirrors server.py's
// core and does not gate requests on ELASTIK_TOKEN either. The env
// var is still read so that a future Go-side auth plugin can pick it
// up, and so that we can warn at startup when running public.

// pathSafe rejects requests that try to traverse the URL.
func pathSafe(p, raw string) bool {
	return !strings.Contains(p, "..") && !strings.Contains(p, "//") &&
		!strings.Contains(raw, "..") && !strings.Contains(raw, "//")
}

// ─── router ──────────────────────────────────────────────────────────

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimRight(r.URL.Path, "/")
	if path == "" {
		path = "/"
	}
	log.Printf("  %s %s", r.Method, path)

	if !pathSafe(path, r.URL.EscapedPath()) {
		writeErr(w, 400, "invalid path")
		return
	}

	// Static file routes (match server.py).
	if r.Method == http.MethodGet && path == "/openapi.json" {
		s.serveOpenAPI(w)
		return
	}
	if r.Method == http.MethodGet && path == "/sw.js" {
		s.serveSW(w)
		return
	}

	// /stages — list every world.
	if r.Method == http.MethodGet && path == "/stages" {
		list, err := core.ListStages(s.db)
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		// server.py returns [] for an empty data dir, not null.
		if list == nil {
			list = []core.StageInfo{}
		}
		writeJSON(w, 200, list)
		return
	}

	parts := splitPath(path)
	// /{name}/{action} routes.
	if len(parts) == 2 {
		s.handleWorld(w, r, parts[0], parts[1])
		return
	}

	// Root — serve index.html (matches server.py GET / fallback).
	if r.Method == http.MethodGet && path == "/" {
		s.serveIndex(w)
		return
	}

	writeErr(w, 404, "not found")
}

func splitPath(p string) []string {
	out := []string{}
	for _, s := range strings.Split(p, "/") {
		if s != "" {
			out = append(out, s)
		}
	}
	return out
}

// handleWorld dispatches /{name}/{action}.
func (s *server) handleWorld(w http.ResponseWriter, r *http.Request, name, action string) {
	validActions := map[string]bool{
		"read": true, "write": true, "append": true,
		"sync": true, "pending": true, "result": true, "clear": true,
	}
	if !validActions[action] {
		writeErr(w, 404, "not found")
		return
	}

	// Validate name up front for consistent 400s.
	if !core.ValidName(name) {
		writeErr(w, 400, "invalid world name")
		return
	}

	// READ — must not create the world.
	if action == "read" {
		if r.Method != http.MethodGet {
			writeErr(w, 405, "method not allowed")
			return
		}
		stage, err := core.ReadWorld(s.db, name)
		if err != nil {
			st, msg := mapError(err)
			writeErr(w, st, msg)
			return
		}
		writeJSON(w, 200, stage)
		return
	}

	// All other actions mutate state — only POST.
	if r.Method != http.MethodPost {
		writeErr(w, 405, "method not allowed")
		return
	}
	body, err := readBody(r)
	if err != nil {
		writeErr(w, 413, "body too large")
		return
	}

	switch action {
	case "write":
		v, err := core.WriteWorld(s.db, s.cfg.key, name, body)
		if err != nil {
			st, msg := mapError(err)
			writeErr(w, st, msg)
			return
		}
		writeJSON(w, 200, map[string]int{"version": v})

	case "append":
		v, err := core.AppendWorld(s.db, s.cfg.key, name, body)
		if err != nil {
			st, msg := mapError(err)
			writeErr(w, st, msg)
			return
		}
		writeJSON(w, 200, map[string]int{"version": v})

	case "sync":
		if err := core.SyncWorld(s.db, name, body); err != nil {
			st, msg := mapError(err)
			writeErr(w, st, msg)
			return
		}
		writeJSON(w, 200, map[string]bool{"ok": true})

	case "pending":
		if err := core.SetPending(s.db, name, body); err != nil {
			st, msg := mapError(err)
			writeErr(w, st, msg)
			return
		}
		writeJSON(w, 200, map[string]bool{"ok": true})

	case "result":
		if err := core.SetResult(s.db, name, body); err != nil {
			st, msg := mapError(err)
			writeErr(w, st, msg)
			return
		}
		writeJSON(w, 200, map[string]bool{"ok": true})

	case "clear":
		if err := core.ClearWorld(s.db, name); err != nil {
			st, msg := mapError(err)
			writeErr(w, st, msg)
			return
		}
		writeJSON(w, 200, map[string]bool{"ok": true})
	}
}

// ─── main ────────────────────────────────────────────────────────────

func main() {
	if path, ok := loadDotEnv(); ok {
		log.Printf("  env: loaded %s", path)
	}
	cfg := loadConfig()
	s := &server{
		cfg:    cfg,
		db:     newSQLiteDB(cfg.dataDir),
		static: loadStatic(),
	}

	addr := fmt.Sprintf("%s:%s", cfg.host, cfg.port)
	log.Printf("  elastik-lite (go) -> http://%s  [protocol + static]", addr)
	log.Printf("  data dir: %s", cfg.dataDir)
	if cfg.token == "" {
		log.Printf("  ! ELASTIK_TOKEN not set — open access (dev mode)")
	}
	if err := http.ListenAndServe(addr, s); err != nil {
		log.Fatal(err)
	}
}
