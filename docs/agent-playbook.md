# Agent Playbook

How AI develops elastik. Recursive. Self-organizing.

---

## The idea

One agent reads the issue list. For each issue, it spawns a child agent.
Each child works in isolation (git worktree). Each child commits, pushes,
opens a PR. The human comes back and reviews a batch of PRs.

The parent agent is a project manager. It doesn't write code.
It reads issues, assigns work, checks results.

The child agents are developers. They read context, write code,
run tests, commit. They don't ask questions unless the design
is ambiguous.

If you understand recursion, you understand this.
If you don't, you don't.

---

## System prompt for the parent agent

```
You are the development lead for the elastik project.

Context:
- Repo: github.com/rangersui/Elastik
- Protocol spec: server.py (~258 lines, Python reference implementation)
- Go implementation: go/ directory
- Docs: README.md + docs/protocol.md
- Issues: GitHub Issues, organized by milestone

Workflow:
1. Read GitHub issues (filter by milestone if given)
2. For each issue that is actionable:
   - Spawn a child agent in an isolated worktree
   - Give it the issue body + relevant file paths + rules
   - Let it work autonomously
3. Each child: read context → write code → run tests → commit → push → open PR
4. You verify: does it build? does the diff make sense? are the files correct?
5. Report to the human: list of PRs ready for review

You do NOT write code yourself. You delegate.
You do NOT ask the human for permission on each issue. You batch.
You DO stop on ambiguous design decisions — note them in the PR description.
```

---

## System prompt for child agents

```
You are a developer on the elastik project.

Your task: [issue title]
Branch: feat/[issue-short-name]
Base: master

Context you must read first:
- server.py — the protocol spec. Your Go/Python code must match its behavior.
- go/core/ — protocol core (HMAC, world operations). Already tested.
- The issue body (pasted below)

Rules:
- One PR, one feature. Don't touch unrelated files.
- Zero dependencies. stdlib only (Go or Python).
- Don't change the protocol core (five rules, three mailboxes, HMAC chain)
  unless the issue explicitly requires it.
- Build must pass. Run `go build ./native` from go/ directory.
- Commit with a clear message. Reference the issue number.
- If a design decision is ambiguous, write it in a code comment
  with `// DECISION:` prefix. Don't guess. Don't ask.

When done:
- git add + git commit + git push
- Open a PR with: summary (what), test plan (how to verify), any DECISION notes
- Exit
```

---

## What the child agent needs to know about elastik

Ten lines. If it understands these, it can build anything in the system.

1. Everything is a string. No types. No schemas.
2. Strings live in worlds. Each world has three mailboxes: stage, pending, result.
3. Stage is what the browser renders. Pending is code the browser executes. Result is what the browser sends back.
4. Every write is HMAC-chained. Append-only. Immutable audit trail.
5. One database file: universe.db (SQLite).
6. The server is ~258 lines. server.py is the spec. Go Lite is the strict reimplementation.
7. Renderers are worlds whose stage_html is loaded by `<!--use:renderer-name-->`.
8. Plugins are Python files that register routes. Go Lite doesn't have plugins.
9. Auth: ELASTIK_TOKEN for AI (read/write), ELASTIK_APPROVE_TOKEN for human (admin). Physics, not policy.
10. Zero dependencies. No frameworks. If it can't run with just the language runtime, it doesn't belong.

---

## File map for child agents

```
server.py             THE SPEC. Read this first. Every route, every behavior.
go/core/              Protocol core: HMAC chain, world CRUD, validation.
go/core/core_test.go  Tests. Run with: go test ./core
go/native/main.go     HTTP router. Routes map 1:1 to server.py.
go/native/ai.go       AI bridge (5 providers). Auto-detect at startup.
go/native/mcp.go      MCP stdio server (--mcp flag).
go/native/static.go   Embedded static files (index.html, sw.js, etc).
index.html            The entire frontend. One file. ~77 lines.
mcp_server.py         MCP aggregator for Python Pro path.
plugins/available/    Plugin source files (Python Pro only).
docs/protocol.md      Formal protocol specification.
.env.example          All supported environment variables.
```

---

## Example: parent agent processes a milestone

```
Human: "Do the v2.1 milestone"

Parent agent:
  1. gh issue list --milestone "v2.1" --state open
  2. Reads 6 issues
  3. Filters: 4 are actionable, 2 need design decisions
  4. Spawns 4 child agents in parallel (worktrees)
  5. Each child: branch → code → test → commit → push → PR
  6. Parent verifies each PR builds
  7. Reports to human:
     "4 PRs ready for review:
      - #40 PWA manifest (3 files, +40 lines)
      - #4  Frontend undo (1 file, +15 lines)
      - #5  MCP config migration (1 file, +46 lines)
      Issues #34 and #39 need design decisions — skipped."
  8. Human reviews, merges or requests changes
  9. Parent picks up next milestone
```

---

## Why this works for elastik specifically

elastik is small. The entire protocol is ~258 lines. An agent can read
the whole spec in one context window. There's no framework to learn,
no build system to fight, no dependency graph to untangle.

If you understand the ten lines above, you can implement any feature.
If you don't, reading more code won't help.

The protocol is the context. The context is the protocol.

---

## Anti-patterns

**Don't**: give a child agent the entire repo and say "figure it out."
Give it the issue body, the file map, and the ten-line summary.

**Don't**: let a child agent modify server.py's core routes without
explicit issue authorization. server.py is the spec. Changing the spec
is a different kind of work.

**Don't**: have the parent agent write code "to save time."
The parent manages. The children execute. Mixing roles creates
unreviewable diffs.

**Don't**: skip the build check. A child agent that pushes broken
code wastes the human's review time. The parent catches this.

**Don't**: merge without human review. The human is the final gate.
AI proposes. Human approves. Always.

---

## The recursion

The parent agent is itself an AI following a prompt.
The child agents are AIs following prompts.
The prompts are strings.
The PRs are strings.
The code is strings.
The protocol moves strings.

elastik is a protocol for moving strings between intelligence and tools.
The development process is itself an instance of the protocol.

The agent that builds elastik is a user of elastik.

If you understand this, you understand elastik.
If you don't, you don't.
