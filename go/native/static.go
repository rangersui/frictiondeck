package main

import (
	"log"
	"net/http"
	"os"
	"path/filepath"
)

// staticFiles holds the three files server.py serves from disk at
// startup: index.html at /, openapi.json at /openapi.json, sw.js at
// /sw.js, manifest.json at /manifest.json. Loaded once in
// loadStatic(), same as server.py's INDEX/OPENAPI/SW module-level
// reads.
//
// Missing files are fine — we log a warning and skip the route. This
// keeps the binary usable even when dropped into a dir without the
// frontend assets (e.g. a headless protocol node).
type staticFiles struct {
	index    []byte
	openapi  []byte
	sw       []byte
	manifest []byte
	icon     []byte
	shell    []byte
	mirror   []byte
}

// loadStatic searches ELASTIK_STATIC, then CWD, then the exe dir, for
// index.html / openapi.json / sw.js / manifest.json. Each file is loaded independently
// from whichever dir finds it first — they don't need to all come from
// the same place.
func loadStatic() staticFiles {
	dirs := []string{}
	if explicit := os.Getenv("ELASTIK_STATIC"); explicit != "" {
		dirs = append(dirs, explicit)
	}
	if cwd, err := os.Getwd(); err == nil {
		dirs = append(dirs, cwd)
	}
	if exe, err := os.Executable(); err == nil {
		dirs = append(dirs, filepath.Dir(exe))
	}
	find := func(name string) []byte {
		for _, d := range dirs {
			b, err := os.ReadFile(filepath.Join(d, name))
			if err == nil {
				log.Printf("  static: %s <- %s", name, filepath.Join(d, name))
				return b
			}
		}
		log.Printf("  static: %s not found (skipping route)", name)
		return nil
	}
	return staticFiles{
		index:    find("index.html"),
		openapi:  find("openapi.json"),
		sw:       find("sw.js"),
		manifest: find("manifest.json"),
		icon:     find("icon.png"),
		shell:    find("shell.html"),
		mirror:   find("mirror.html"),
	}
}

// serveIndex writes index.html with the same minimal CSP server.py
// uses. We don't read the config-cdn world here — that's a plugin-layer
// concern; Go protocol server uses a permissive default matching
// server.py's fallback branch.
func (s *server) serveIndex(w http.ResponseWriter) {
	if s.static.index == nil {
		writeJSON(w, 200, map[string]any{
			"elastik": "go-lite",
			"version": "2.0-dev",
			"note":    "index.html not found — drop exe next to it or set ELASTIK_STATIC",
		})
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Security-Policy",
		"default-src 'self' data: blob:; "+
			"script-src 'unsafe-inline' 'unsafe-eval' https: data:; "+
			"style-src 'unsafe-inline' https: data:; "+
			"img-src * data: blob:; font-src * data:; "+
			"connect-src 'self'; worker-src 'self'")
	w.WriteHeader(200)
	_, _ = w.Write(s.static.index)
}

func (s *server) serveOpenAPI(w http.ResponseWriter) {
	if s.static.openapi == nil {
		writeErr(w, 404, "openapi.json not found")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(200)
	_, _ = w.Write(s.static.openapi)
}

func (s *server) serveSW(w http.ResponseWriter) {
	if s.static.sw == nil {
		writeErr(w, 404, "sw.js not found")
		return
	}
	w.Header().Set("Content-Type", "application/javascript")
	w.WriteHeader(200)
	_, _ = w.Write(s.static.sw)
}

func (s *server) serveIcon(w http.ResponseWriter) {
	if s.static.icon == nil {
		writeErr(w, 404, "icon.png not found")
		return
	}
	w.Header().Set("Content-Type", "image/png")
	w.WriteHeader(200)
	_, _ = w.Write(s.static.icon)
}

func (s *server) serveMirror(w http.ResponseWriter) {
	if s.static.mirror == nil {
		writeErr(w, 404, "mirror.html not found")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(200)
	_, _ = w.Write(s.static.mirror)
}

func (s *server) serveShell(w http.ResponseWriter) {
	if s.static.shell == nil {
		writeErr(w, 404, "shell.html not found")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(200)
	_, _ = w.Write(s.static.shell)
}

func (s *server) serveManifest(w http.ResponseWriter) {
	if s.static.manifest == nil {
		writeErr(w, 404, "manifest.json not found")
		return
	}
	w.Header().Set("Content-Type", "application/manifest+json")
	w.WriteHeader(200)
	_, _ = w.Write(s.static.manifest)
}
