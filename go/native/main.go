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
	"context"
	"crypto/hmac"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
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

// checkAuth mirrors Python's _check_auth(scope).
// Authorization: Bearer TOKEN or Basic :TOKEN → "approve", "auth", or "".
func (s *server) checkAuth(r *http.Request) string {
	auth := r.Header.Get("Authorization")
	if strings.HasPrefix(auth, "Bearer ") {
		tok := auth[7:]
		if s.cfg.approveToken != "" && hmacEqual(tok, s.cfg.approveToken) {
			return "approve"
		}
		if s.cfg.token != "" && hmacEqual(tok, s.cfg.token) {
			return "auth"
		}
		return ""
	}
	_, password, ok := r.BasicAuth()
	if ok {
		if s.cfg.approveToken != "" && hmacEqual(password, s.cfg.approveToken) {
			return "approve"
		}
		if s.cfg.token != "" && hmacEqual(password, s.cfg.token) {
			return "auth"
		}
		return ""
	}
	return ""
}

// ─── WebDAV ─────────────────────────────────────────────────────────

// davWorldName extracts world name from /dav/xyz.html → "xyz".
// Strips any single extension (.html, .css, .js, etc.) since world
// names never contain dots.
// Returns "" if path is just /dav or /dav/.
func davWorldName(path string) string {
	if path == "/dav" || path == "/dav/" {
		return ""
	}
	rest := strings.TrimPrefix(path, "/dav/")
	if i := strings.LastIndex(rest, "."); i > 0 {
		return rest[:i]
	}
	return rest
}

// davTypeRe matches <!--type:xxx--> anywhere in content.
var davTypeRe = regexp.MustCompile(`<!--type:(\w+)-->`)

// davFileExt returns the file extension for a world based on its content.
// <!--type:css--> → ".css", <!--type:js--> → ".js", default → ".html".
// Worlds using <!--use:renderer--> are always ".html" (renderer produces HTML).
func davFileExt(stageHTML string) string {
	if strings.HasPrefix(stageHTML, "<!--use:") {
		return ".html"
	}
	if m := davTypeRe.FindStringSubmatch(stageHTML); m != nil {
		return "." + m[1]
	}
	return ".html"
}

// davPropEntry generates a single <D:response> XML block.
func davPropEntry(href, restype, ct string, size int, modified string) string {
	rt := "<D:resourcetype/>"
	if restype == "collection" {
		rt = "<D:resourcetype><D:collection/></D:resourcetype>"
	}
	ctLine := ""
	if ct != "" {
		ctLine = "<D:getcontenttype>" + ct + "</D:getcontenttype>"
	}
	return "<D:response><D:href>" + href + "</D:href><D:propstat><D:prop>" +
		rt + "<D:getcontentlength>" + fmt.Sprintf("%d", size) + "</D:getcontentlength>" +
		"<D:getlastmodified>" + modified + "</D:getlastmodified>" +
		ctLine + "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
}

func (s *server) handleDAV(w http.ResponseWriter, r *http.Request, path string) {
	// Auth: same as normal routes.
	// Read (OPTIONS, PROPFIND, GET, HEAD) → open (like GET /stages, GET /{w}/read)
	// Write (PUT, DELETE) → auth token (like POST /{w}/write)
	isWrite := r.Method == "PUT" || r.Method == "DELETE"
	if isWrite && s.cfg.token != "" {
		if s.checkAuth(r) == "" {
			w.Header().Set("WWW-Authenticate", `Basic realm="elastik"`)
			writeErr(w, 401, "unauthorized")
			return
		}
	}

	switch r.Method {
	case "OPTIONS":
		w.Header().Set("DAV", "1")
		w.Header().Set("Allow", "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND")
		w.WriteHeader(200)

	case "PROPFIND":
		depth := r.Header.Get("Depth")
		if depth == "" {
			depth = "1"
		}
		name := davWorldName(path)
		now := time.Now().UTC().Format(http.TimeFormat)

		if name != "" {
			// Single resource: /dav/work.css
			if !core.ValidName(name) {
				writeErr(w, 400, "invalid world name")
				return
			}
			stage, err := core.ReadWorld(s.db, name)
			if err != nil {
				writeErr(w, 404, "not found")
				return
			}
			ext := davFileExt(stage.StageHTML)
			xml := `<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">` +
				davPropEntry("/dav/"+name+ext, "", "text/plain", len(stage.StageHTML), now) +
				`</D:multistatus>`
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(207)
			_, _ = w.Write([]byte(xml))
			return
		}

		// Root collection
		xml := `<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">` +
			davPropEntry("/dav/", "collection", "", 0, now)
		if depth == "1" {
			list, err := core.ListStages(s.db)
			if err == nil {
				for _, st := range list {
					stage, err := core.ReadWorld(s.db, st.Name)
					if err != nil {
						continue
					}
					xml += davPropEntry("/dav/"+st.Name+davFileExt(stage.StageHTML), "", "text/plain", len(stage.StageHTML), now)
				}
			}
		}
		xml += `</D:multistatus>`
		w.Header().Set("Content-Type", "application/xml; charset=utf-8")
		w.WriteHeader(207)
		_, _ = w.Write([]byte(xml))

	case "GET", "HEAD":
		name := davWorldName(path)
		if name == "" {
			// Browser hitting /dav/ — list worlds as simple HTML
			list, err := core.ListStages(s.db)
			if err != nil {
				writeErr(w, 500, err.Error())
				return
			}
			html := "<h1>elastik WebDAV</h1><ul>"
			for _, st := range list {
				stage, err := core.ReadWorld(s.db, st.Name)
				ext := ".html"
				if err == nil {
					ext = davFileExt(stage.StageHTML)
				}
				html += `<li><a href="/dav/` + st.Name + ext + `">` + st.Name + `</a></li>`
			}
			html += "</ul>"
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.WriteHeader(200)
			_, _ = w.Write([]byte(html))
			return
		}
		if !core.ValidName(name) {
			writeErr(w, 400, "invalid world name")
			return
		}
		stage, err := core.ReadWorld(s.db, name)
		if err != nil {
			writeErr(w, 404, "not found")
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		if r.Method == "HEAD" {
			w.Header().Set("Content-Length", fmt.Sprintf("%d", len(stage.StageHTML)))
			w.WriteHeader(200)
		} else {
			w.WriteHeader(200)
			_, _ = w.Write([]byte(stage.StageHTML))
		}

	case "PUT":
		name := davWorldName(path)
		if name == "" {
			writeErr(w, 405, "PUT on collection not supported")
			return
		}
		if !core.ValidName(name) {
			writeErr(w, 400, "invalid world name")
			return
		}
		body, err := readBody(r)
		if err != nil {
			writeErr(w, 413, "body too large")
			return
		}
		_, err = core.WriteWorld(s.db, s.cfg.key, name, body)
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		w.WriteHeader(201)

	case "DELETE":
		name := davWorldName(path)
		if name == "" {
			writeErr(w, 405, "DELETE on collection not supported")
			return
		}
		if !core.ValidName(name) {
			writeErr(w, 400, "invalid world name")
			return
		}
		if err := core.ClearWorld(s.db, name); err != nil {
			st, msg := mapError(err)
			writeErr(w, st, msg)
			return
		}
		w.WriteHeader(204)

	case "MKCOL":
		writeErr(w, 405, "worlds are flat — no subdirectories")

	case "LOCK", "UNLOCK":
		w.WriteHeader(501)

	default:
		writeErr(w, 405, "method not allowed")
	}
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

	// WebDAV: /dav/ namespace — worlds as files.
	if path == "/dav" || strings.HasPrefix(path, "/dav/") {
		s.handleDAV(w, r, path)
		return
	}

	// Mirror: /mirror?url=X (entry) or /m/domain/path (subsequent).
	if target, domain, ok := mirrorTarget(path, r.URL.RawQuery); ok {
		if s.checkAuth(r) != "approve" {
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
		if domain != "" && s.checkAuth(r) == "approve" {
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
	//   Tier 1: admin + config-* + /proxy/postman → require approve
	//   Tier 2: all other POST/PUT/DELETE → require auth or approve
	//   GET is always open.
	if r.Method != http.MethodGet {
		level := s.checkAuth(r)
		parts := splitPath(path)
		isAdmin := strings.HasPrefix(path, "/admin/")
		isConfig := len(parts) >= 1 && strings.HasPrefix(parts[0], "config-")
		isPostman := strings.HasPrefix(path, "/proxy")
		if isAdmin || isConfig || isPostman {
			// Tier 1: approve required.
			if level != "approve" {
				writeErr(w, 403, "unauthorized")
				return
			}
		} else if s.cfg.token != "" {
			// Tier 2: any auth level for normal writes.
			if level == "" {
				writeErr(w, 403, "unauthorized")
				return
			}
		}
	}

	// Static file routes (match server.py).
	if r.Method == http.MethodGet && path == "/opensearch.xml" {
		host := r.Host
		scheme := "http"
		if r.TLS != nil {
			scheme = "https"
		}
		xml := `<?xml version="1.0" encoding="UTF-8"?>` +
			`<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">` +
			`<ShortName>elastik</ShortName><Description>elastik shell</Description>` +
			`<Url type="text/html" template="` + scheme + `://` + host + `/shell?q={searchTerms}"/>` +
			`</OpenSearchDescription>`
		w.Header().Set("Content-Type", "application/opensearchdescription+xml")
		w.WriteHeader(200)
		_, _ = w.Write([]byte(xml))
		return
	}
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
			user, _, _ := r.BasicAuth()
			s.serveShellWithUser(w, user)
		} else {
			s.serveMirror(w)
		}
		return
	}

	// POST /exec — system shell, approve token protected.
	if r.Method == http.MethodPost && path == "/exec" {
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
		body, err := readBody(r)
		if err != nil {
			writeErr(w, 413, "body too large")
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
		defer cancel()
		var cmd *exec.Cmd
		if runtime.GOOS == "windows" {
			cmd = exec.CommandContext(ctx, "powershell", "-Command", body)
		} else {
			cmd = exec.CommandContext(ctx, "bash", "-c", body)
		}
		out, _ := cmd.CombinedOutput()
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(200)
		_, _ = w.Write(out)
		return
	}

	// /view/{world} — root view: direct HTML render, Basic Auth protected.
	if r.Method == http.MethodGet && strings.HasPrefix(path, "/view/") {
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
		name := path[6:] // /view/work → work
		if !core.ValidName(name) {
			writeErr(w, 400, "invalid world name")
			return
		}
		stage, err := core.ReadWorld(s.db, name)
		if err != nil {
			st, msg := mapError(err)
			writeErr(w, st, msg)
			return
		}
		html := stage.StageHTML
		if html == "" {
			html = "<em>(empty)</em>"
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(200)
		_, _ = w.Write([]byte(html))
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
