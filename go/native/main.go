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
	"crypto/hmac"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/elastik/go/core"
)

// hmacEqual does constant-time comparison to prevent timing attacks.
func hmacEqual(a, b string) bool {
	return hmac.Equal([]byte(a), []byte(b))
}

// ─── config ──────────────────────────────────────────────────────────

type config struct {
	host         string
	port         string
	dataDir      string
	key          []byte
	token        string
	approveToken string
}

func loadConfig() config {
	c := config{
		host:    env("ELASTIK_HOST", "127.0.0.1"),
		port:    env("ELASTIK_PORT", "3005"),
		dataDir: env("ELASTIK_DATA", "data"),
		key:          []byte(env("ELASTIK_KEY", "elastik-dev-key")),
		token:        os.Getenv("ELASTIK_TOKEN"),
		approveToken: os.Getenv("ELASTIK_APPROVE_TOKEN"),
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

// getApproveToken extracts the approve token from X-Approve-Token header
// or Basic Auth password (for /shell browser sessions).
func getApproveToken(r *http.Request) string {
	if t := r.Header.Get("X-Approve-Token"); t != "" {
		return t
	}
	_, password, ok := r.BasicAuth()
	if ok {
		return password
	}
	return ""
}

// ─── mirror reverse proxy ────────────────────────────────────────────

var mirrorClient = &http.Client{Timeout: 30 * time.Second}
var metaCSPRe = regexp.MustCompile(`(?i)<meta[^>]*(?:content-security-policy|x-frame-options)[^>]*>`)

// mirrorProxy fetches target URL and writes response. For HTML, injects <base> and
// strips <meta> CSP tags. Origin headers are never forwarded — only Content-Type.
func mirrorProxy(w http.ResponseWriter, target, domain string) {
	req, err := http.NewRequest("GET", target, nil)
	if err != nil {
		writeErr(w, 502, "proxy error: "+err.Error())
		return
	}
	req.Header.Del("User-Agent")
	resp, err := mirrorClient.Do(req)
	if err != nil {
		writeErr(w, 502, "proxy error: "+err.Error())
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, maxBody))
	ct := resp.Header.Get("Content-Type")
	if ct == "" {
		ct = "text/html; charset=utf-8"
	}
	if strings.Contains(ct, "text/html") && domain != "" {
		// Strip <meta> CSP/X-Frame-Options — you're root, you decide security policy.
		body = metaCSPRe.ReplaceAll(body, nil)
		// Inject <base> so relative URLs stay in /m/domain/ namespace.
		base := []byte(`<base href="/m/` + domain + `/">`)
		body = append(base, body...)
	}
	w.Header().Set("Content-Type", ct)
	w.WriteHeader(200)
	_, _ = w.Write(body)
}

// mirrorTarget parses a mirror URL and returns (full URL, domain).
// /mirror?url=https://github.com → ("https://github.com", "github.com")
// /m/github.com/features         → ("https://github.com/features", "github.com")
func mirrorTarget(path, rawQuery string) (target, domain string, ok bool) {
	// Entry: /mirror?url=X
	if path == "/mirror" || path == "/mirror/" {
		u, _ := url.ParseQuery(rawQuery)
		raw := u.Get("url")
		if raw == "" || (!strings.HasPrefix(raw, "http://") && !strings.HasPrefix(raw, "https://")) {
			return "", "", false
		}
		parsed, err := url.Parse(raw)
		if err != nil {
			return "", "", false
		}
		return raw, parsed.Host, true
	}
	// Subsequent: /m/domain/path
	if strings.HasPrefix(path, "/m/") {
		rest := path[3:] // strip "/m/"
		slash := strings.Index(rest, "/")
		if slash == -1 {
			return "https://" + rest, rest, true
		}
		dom := rest[:slash]
		p := rest[slash:]
		target = "https://" + dom + p
		if rawQuery != "" {
			target += "?" + rawQuery
		}
		return target, dom, true
	}
	return "", "", false
}

// ─── path guards ─────────────────────────────────────────────────────

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

	// Mirror: /mirror?url=X (entry) or /m/domain/path (subsequent).
	if target, domain, ok := mirrorTarget(path, r.URL.RawQuery); ok {
		if s.cfg.approveToken == "" || !hmacEqual(getApproveToken(r), s.cfg.approveToken) {
			w.Header().Set("WWW-Authenticate", `Basic realm="elastik"`)
			writeErr(w, 401, "authentication required")
			return
		}
		mirrorProxy(w, target, domain)
		return
	}
	// Mirror Referer fallback — absolute paths (/search) from mirrored pages.
	// <base> only fixes relative URLs; absolute paths need Referer to find the domain.
	if ref := r.Header.Get("Referer"); ref != "" {
		domain := ""
		if idx := strings.Index(ref, "/m/"); idx >= 0 {
			rest := ref[idx+3:]
			if slash := strings.Index(rest, "/"); slash > 0 {
				rest = rest[:slash]
			}
			if qm := strings.Index(rest, "?"); qm > 0 {
				rest = rest[:qm]
			}
			domain = rest
		} else if idx := strings.Index(ref, "/mirror?url="); idx >= 0 {
			raw := ref[idx+12:]
			if decoded, err := url.QueryUnescape(raw); err == nil {
				if parsed, err := url.Parse(decoded); err == nil {
					domain = parsed.Host
				}
			}
		}
		if domain != "" && s.cfg.approveToken != "" && hmacEqual(getApproveToken(r), s.cfg.approveToken) {
			if r.Method == http.MethodGet {
				// GET: 302 redirect to /m/domain/path — pull URL back into namespace.
				redir := "/m/" + domain + path
				if qs := r.URL.RawQuery; qs != "" {
					redir += "?" + qs
				}
				http.Redirect(w, r, redir, http.StatusFound)
			} else {
				// POST: proxy directly — redirect would lose body.
				target := "https://" + domain + path
				if qs := r.URL.RawQuery; qs != "" {
					target += "?" + qs
				}
				mirrorProxy(w, target, domain)
			}
			return
		}
	}

	// Auth — two tiers, mirrors plugins/auth.py:
	//   Tier 1: admin + config-* + /proxy/postman → require X-Approve-Token
	//   Tier 2: all other POST/PUT/DELETE → require X-Auth-Token
	//   GET is always open.
	if r.Method != http.MethodGet {
		parts := splitPath(path)
		isAdmin := strings.HasPrefix(path, "/admin/")
		isConfig := len(parts) >= 1 && strings.HasPrefix(parts[0], "config-")
		isPostman := strings.HasPrefix(path, "/proxy")
		if isAdmin || isConfig || isPostman {
			// Tier 1: approve token required — locked if not set.
			if s.cfg.approveToken == "" || !hmacEqual(getApproveToken(r), s.cfg.approveToken) {
				writeErr(w, 403, "unauthorized")
				return
			}
		} else if s.cfg.token != "" {
			// Tier 2: auth token for normal writes.
			// Approve token (via header or Basic Auth) also passes — higher privilege.
			authOk := hmacEqual(r.Header.Get("X-Auth-Token"), s.cfg.token)
			approveOk := s.cfg.approveToken != "" && hmacEqual(getApproveToken(r), s.cfg.approveToken)
			if !authOk && !approveOk {
				writeErr(w, 403, "unauthorized")
				return
			}
		}
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
	if r.Method == http.MethodGet && path == "/manifest.json" {
		s.serveManifest(w)
		return
	}
	if r.Method == http.MethodGet && path == "/icon.png" {
		s.serveIcon(w)
		return
	}

	// /shell, /mirror — Basic Auth protected root pages.
	if r.Method == http.MethodGet && (path == "/shell" || path == "/mirror") {
		if s.cfg.approveToken == "" {
			writeErr(w, 403, "approve token not configured")
			return
		}
		_, pass, ok := r.BasicAuth()
		if !ok || !hmacEqual(pass, s.cfg.approveToken) {
			w.Header().Set("WWW-Authenticate", `Basic realm="elastik"`)
			writeErr(w, 401, "authentication required")
			return
		}
		if path == "/shell" {
			s.serveShell(w)
		} else {
			s.serveMirror(w)
		}
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

	// Hot reload endpoint.
	if path == "/plugins/reload" && r.Method == http.MethodPost {
		scanPlugins()
		routeMu.RLock()
		names := make([]string, 0, len(routeTable))
		for route := range routeTable {
			names = append(names, route)
		}
		routeMu.RUnlock()
		writeJSON(w, 200, map[string]any{"ok": true, "routes": names})
		return
	}
	if path == "/plugins/list" && r.Method == http.MethodGet {
		routeMu.RLock()
		type entry struct {
			Route  string `json:"route"`
			Plugin string `json:"plugin"`
		}
		var list []entry
		for route, plug := range routeTable {
			list = append(list, entry{route, filepath.Base(plug)})
		}
		routeMu.RUnlock()
		writeJSON(w, 200, list)
		return
	}

	// Plugin routes — declared by plugins via --routes at startup.
	routeMu.RLock()
	p, ok := routeTable[path]
	routeMu.RUnlock()
	if ok {
		servePlugin(w, r, p, path)
		return
	}

	parts := splitPath(path)

	// /{name}/{action} routes.
	if len(parts) == 2 {
		s.handleWorld(w, r, parts[0], parts[1])
		return
	}

	// GET fallback — matches server.py's final `if method == "GET"`
	// branch. Serves index.html for / and for any single-segment world
	// path (/work, /home, etc.). The browser entry point; JS inside
	// index.html then calls /{name}/read to fetch the world data.
	if r.Method == http.MethodGet && (path == "/" || len(parts) == 1) {
		// Guard: reject single-segment paths that aren't valid world
		// names so we don't hand index.html out on garbage URLs.
		if len(parts) == 1 && !core.ValidName(parts[0]) {
			writeErr(w, 400, "invalid world name")
			return
		}
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

	// Export absolute data/root paths so CGI plugins don't have to guess.
	// Plugins read $ELASTIK_DATA instead of counting __file__ parents.
	if absData, err := filepath.Abs(cfg.dataDir); err == nil {
		os.Setenv("ELASTIK_DATA", absData)
	}
	if absRoot, err := filepath.Abs("."); err == nil {
		os.Setenv("ELASTIK_ROOT", absRoot)
	}

	s := &server{
		cfg:    cfg,
		db:     newSQLiteDB(cfg.dataDir),
		static: loadStatic(),
	}

	scanPlugins()

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
