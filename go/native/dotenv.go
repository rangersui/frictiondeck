package main

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"
)

// loadDotEnv mirrors server.py's dotenv loader: tries .env, _env,
// .env.local in order (iOS doesn't allow dotfiles, hence _env), stops
// at the first one found, and sets any key that isn't already in the
// environment. Same semantics as python's os.environ.setdefault.
//
// Lookup order for the file:
//  1. $ELASTIK_ENV (explicit override)
//  2. current working directory
//  3. directory of the running executable
//
// This matches the "drop the exe next to .env" workflow.
func loadDotEnv() (string, bool) {
	if explicit := os.Getenv("ELASTIK_ENV"); explicit != "" {
		if applyEnvFile(explicit) {
			return explicit, true
		}
	}
	dirs := []string{}
	if cwd, err := os.Getwd(); err == nil {
		dirs = append(dirs, cwd)
	}
	if exe, err := os.Executable(); err == nil {
		dirs = append(dirs, filepath.Dir(exe))
	}
	for _, d := range dirs {
		for _, name := range []string{".env", "_env", ".env.local"} {
			p := filepath.Join(d, name)
			if applyEnvFile(p) {
				return p, true
			}
		}
	}
	return "", false
}

func applyEnvFile(path string) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		eq := strings.IndexByte(line, '=')
		if eq < 0 {
			continue
		}
		k := strings.TrimSpace(line[:eq])
		v := strings.TrimSpace(line[eq+1:])
		// Strip surrounding quotes if present (KEY="value" style).
		if len(v) >= 2 {
			if (v[0] == '"' && v[len(v)-1] == '"') ||
				(v[0] == '\'' && v[len(v)-1] == '\'') {
				v = v[1 : len(v)-1]
			}
		}
		if k == "" {
			continue
		}
		if _, exists := os.LookupEnv(k); !exists {
			_ = os.Setenv(k, v)
		}
	}
	return true
}
