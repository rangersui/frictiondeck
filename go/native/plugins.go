package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const pluginTimeout = 30 * time.Second
const maxPluginOut = 5 << 20 // 5 MB

// Plugin protocol (5 rules):
// 1. Plugin is an executable in plugins/
// 2. plugin --routes → JSON array of routes it handles
// 3. Request: stdin one line JSON {"path","method","body","query"}
// 4. Response: stdout one line JSON {"status","body"}
// 5. Exit 0 → normal, non-zero → 502

type pluginReq struct {
	Path   string `json:"path"`
	Method string `json:"method"`
	Body   string `json:"body"`
	Query  string `json:"query,omitempty"`
}

type pluginResp struct {
	Status int    `json:"status"`
	Body   string `json:"body"`
	CT     string `json:"content_type,omitempty"`
}

// routeTable maps route path → plugin executable path.
var (
	routeTable = map[string]string{}
	routeMu    sync.RWMutex
)

// pluginCmd builds an exec.Cmd for a plugin. .py files run via python.
func pluginCmd(ctx context.Context, path string, args ...string) *exec.Cmd {
	if strings.HasSuffix(path, ".py") {
		return exec.CommandContext(ctx, "python", append([]string{"-u", path}, args...)...)
	}
	return exec.CommandContext(ctx, path, args...)
}

// pluginExec runs a plugin with args and returns stdout. Stderr silenced.
// Used for --routes discovery (short-lived, small output).
func pluginExec(path string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := pluginCmd(ctx, path, args...)
	cmd.Stderr = nil // suppress noise from non-CGI plugins
	return cmd.Output()
}

// scanPlugins runs executables in plugins/ and plugins/available/ with --routes.
func scanPlugins() {
	// Build new table without holding the lock (plugin execs can be slow).
	newTable := map[string]string{}
	for _, dir := range []string{"plugins", filepath.Join("plugins", "available")} {
		entries, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		for _, e := range entries {
			if e.IsDir() || strings.HasPrefix(e.Name(), ".") || strings.HasPrefix(e.Name(), "_") {
				continue
			}
			skip := false
			for _, ext := range []string{".md", ".txt", ".json", ".lock", ".cmd", ".bat"} {
				if strings.HasSuffix(e.Name(), ext) {
					skip = true
					break
				}
			}
			if skip {
				continue
			}
			p := filepath.Join(dir, e.Name())
			out, err := pluginExec(p, "--routes")
			if err != nil {
				continue
			}
			var routes []string
			if json.Unmarshal([]byte(strings.TrimSpace(string(out))), &routes) != nil {
				continue
			}
			// plugins/ takes priority over plugins/available/.
			for _, r := range routes {
				if _, exists := newTable[r]; !exists {
					newTable[r] = p
				}
			}
			log.Printf("  plugin: %s → %v", e.Name(), routes)
		}
	}
	// Swap atomically — lock only held for the pointer swap.
	routeMu.Lock()
	routeTable = newTable
	routeMu.Unlock()
}

// servePlugin dispatches an HTTP request to the plugin executable.
func servePlugin(w http.ResponseWriter, r *http.Request, pluginPath, route string) {
	var body string
	if r.Method == http.MethodPost {
		r.Body = http.MaxBytesReader(nil, r.Body, maxBody)
		b, err := readBody(r)
		if err != nil {
			writeErr(w, 413, "body too large")
			return
		}
		body = b
	}

	reqJSON, _ := json.Marshal(pluginReq{
		Path:   route,
		Method: r.Method,
		Body:   body,
		Query:  r.URL.RawQuery,
	})

	ctx, cancel := context.WithTimeout(r.Context(), pluginTimeout)
	defer cancel()

	cmd := pluginCmd(ctx, pluginPath)
	cmd.Stdin = strings.NewReader(string(reqJSON) + "\n")
	cmd.Stderr = os.Stderr

	// Capture stdout with size limit to prevent OOM from malicious plugins.
	pr, pw := io.Pipe()
	cmd.Stdout = pw
	err := cmd.Start()
	if err != nil {
		log.Printf("  plugin %s start: %v", filepath.Base(pluginPath), err)
		writeErr(w, 502, "plugin error")
		return
	}

	// Read stdout in background, capped at maxPluginOut+1 to detect overflow.
	type readResult struct {
		data []byte
		err  error
	}
	ch := make(chan readResult, 1)
	go func() {
		d, e := io.ReadAll(io.LimitReader(pr, maxPluginOut+1))
		if len(d) > maxPluginOut {
			pr.Close() // break pipe → unblock cmd.Wait() immediately
		}
		ch <- readResult{d, e}
	}()

	waitErr := cmd.Wait()
	pw.Close()
	res := <-ch

	// Check overflow first — a plugin killed because the pipe broke after
	// hitting the 5 MB limit looks like a timeout or exit-error, but the
	// real cause is oversized output.
	if len(res.data) > maxPluginOut {
		log.Printf("  plugin %s: output too large (>%d bytes)", filepath.Base(pluginPath), maxPluginOut)
		writeErr(w, 413, "plugin output too large")
		return
	}
	if waitErr != nil {
		if ctx.Err() == context.DeadlineExceeded {
			log.Printf("  plugin %s: timeout after %v", filepath.Base(pluginPath), pluginTimeout)
			writeErr(w, 504, "plugin timeout")
		} else {
			log.Printf("  plugin %s: %v", filepath.Base(pluginPath), waitErr)
			writeErr(w, 502, "plugin error")
		}
		return
	}
	out := bytes.TrimSpace(res.data)

	var resp pluginResp
	if json.Unmarshal(out, &resp) != nil {
		// Not JSON → raw text response.
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(200)
		_, _ = w.Write(out)
		return
	}

	ct := resp.CT
	if ct == "" {
		ct = "application/json"
	}
	status := resp.Status
	if status == 0 {
		status = 200
	}
	w.Header().Set("Content-Type", ct)
	w.WriteHeader(status)
	_, _ = w.Write([]byte(resp.Body))
}
