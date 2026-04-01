# Blackboard Architecture — Ghost Scripts + Emergent Intelligence

## Concept
Multiple AI agents read/write the same worlds without coordination; intelligence emerges from shared state, not message passing.

## Design

The blackboard pattern treats worlds as shared memory and agents as independent processes. No agent knows about any other agent. Coordination is entirely implicit through world state.

### Agent loop (identical for every agent)

```python
# agent.py — generic blackboard participant
import time, urllib.request, json

BUS = "http://localhost:3004"

def read_world(name):
    r = urllib.request.urlopen(f"{BUS}/{name}/read")
    return json.loads(r.read())

def write_world(name, content):
    req = urllib.request.Request(f"{BUS}/{name}/write",
        data=content.encode(), method="POST")
    urllib.request.urlopen(req)

while True:
    state = read_world("sensor-data")
    if should_act(state["stage_html"]):
        result = do_work(state["stage_html"])
        write_world("analysis-output", result)
    time.sleep(2)
```

Each agent is a dumb loop: read, think, write, sleep. The world schema decides what emerges.

### Example: three-agent pipeline

```
Agent A (sensor)     →  writes raw data     →  world: sensor-data
Agent B (analyst)    →  reads sensor-data    →  writes to: analysis
Agent C (alerter)    →  reads analysis       →  writes to: alerts
```

No agent references another. If Agent B dies, Agent A and C keep working. If a fourth agent appears and starts writing to `analysis`, Agent C picks up its output too. The system scales by adding agents, not by editing wiring.

### Ghost scripts — cross-agent code injection

`pending_js` in `stage_meta` is a command mailbox. One agent writes JavaScript; the browser (or another agent) executes it. The result lands in `js_result`.

```python
# Agent A injects code for the browser to run
write_pending("dashboard", "document.querySelectorAll('.error').length")

# Later, any agent (or Agent A itself) reads the result
r = read_world("dashboard")
error_count = int(r["js_result"])  # browser wrote this back
```

This is already implemented in index.html (poll loop) and tyrant/index.html (`_elastik_exec` postMessage). The blackboard pattern makes it a general-purpose inter-agent RPC where the browser is just another agent.

### World as OS process table

```
world: agent-registry
stage_html:
  agent-a: alive, last_seen=2024-01-15T10:00:00
  agent-b: alive, last_seen=2024-01-15T10:00:01
  agent-c: dead,  last_seen=2024-01-15T09:55:00
```

Each agent appends its heartbeat. Any agent can read the registry to discover who else is working. Still no direct communication — the world is the only shared surface.

### Conflict handling

Agents writing to the same world will overwrite each other (last writer wins, same as sync.py). For append-only workflows, use `POST /{name}/append` instead of `/write`. For structured data, agents write to separate worlds and a reducer agent merges them.

## Implementation estimate
- Agent runner script: ~30 lines (Python, wraps the poll loop)
- Agent registry world + heartbeat: ~15 lines per agent
- Ghost script coordinator (optional): ~20 lines
- No changes to server.py or bus.py required — worlds already support this pattern

## Trigger
When running multiple AI instances simultaneously (Claude via MCP + Ollama via API + WebLLM in browser). Each becomes an agent on the blackboard. The first test case is having Claude write analysis that WebLLM summarizes for display.

## Related
- `pending_js` / `js_result` in `stage_meta` (server.py line 60-62)
- Poll loop in index.html (1-second interval, checks `pending_js`)
- `_elastik_exec` postMessage handler in tyrant/index.html (line 241)
- `POST /{name}/append` for non-destructive multi-agent writes
- sync.py conflict resolution (high version wins)
