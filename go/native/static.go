package main

import (
	"log"
	"net/http"
	"os"
	"path/filepath"
)

// staticFiles holds the three files server.py serves from disk at
// startup: index.html at /, openapi.json at /openapi.json, sw.js at
// /sw.js. Loaded once in loadStatic(), same as server.py's
// INDEX/OPENAPI/SW module-level reads.
//
// Missing files are fine — we log a warning and skip the route. This
// keeps the binary usable even when dropped into a dir without the
// frontend assets (e.g. a headless protocol node).
type staticFiles struct {
	index   []byte
	openapi []byte
	sw      []byte
}

// loadStatic searches ELASTIK_STATIC, then CWD, then the exe dir, for
// index.html / openapi.json / sw.js. Each file is loaded independently
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
		index:   find("index.html"),
		openapi: find("openapi.json"),
		sw:      find("sw.js"),
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
