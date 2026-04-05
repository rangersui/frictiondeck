# go/wasm — browser node (coming in v3.0)

This directory is a placeholder. See `docs/browser-node.md` for the
full design.

## What goes here

A second `package main` that imports `github.com/elastik/go/core` and
wires it to a browser runtime instead of a native HTTP server:

- `main.go` — `GOOS=js GOARCH=wasm` entry point, exposes `core`
  functions to JavaScript via `syscall/js`
- `db_opfs.go` — `core.DB` implementation backed by SQLite WASM over
  OPFS (Origin Private File System)
- Worker glue (JS) lives outside this directory, in the static assets
  served by the native binary

The goal is one `core/` package, two compile targets:

```bash
# Native (already works, v2.0)
go build -o elastik-lite ./native

# Browser (v3.0)
GOOS=js GOARCH=wasm go build -o ../../static/elastik.wasm ./wasm
```

Same HMAC chain. Same schema. Same validation rules. Byte-identical
by construction — not "mostly compatible."

## Pre-3.0 validation spikes

Three ~1-day spikes gate the v3.0 commitment:

1. **TinyGo vs Go bundle size** — TinyGo gives ~10× smaller WASM but
   loses parts of stdlib. Must verify `core/` dependencies fit.
2. **SQLite WASM + OPFS integration** — minimal prototype: open a DB,
   write a row, reopen, read it back. Measure first-write latency and
   binary size.
3. **Worker message bridge** — 50 lines of JS glue: main thread
   postMessage → Worker → Go WASM → SQLite WASM → OPFS → reply.
   End-to-end latency measurement.

All three green → 3.0 is mechanical. Any one blocked → redesign first.

## Not started yet

Don't `go build` this package. It's intentionally empty.
