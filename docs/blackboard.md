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

### Ghost scripts — cross-agent coordination via worlds

Agents coordinate exclusively through world read/write. One agent writes a result to a world; another agent reads it. No special mechanisms — just the same `read_world()` / `write_world()` that every agent uses.

```python
# Agent A writes a command to a coordination world
write_world("commands", json.dumps({"action": "analyze", "target": "sensor-data"}))

# Agent B reads the command world and acts on it
cmd = json.loads(read_world("commands")["stage_html"])
if cmd["action"] == "analyze":
    result = do_analysis(read_world(cmd["target"]))
    write_world("analysis-output", result)
```

No RPC, no message passing, no special fields. Worlds are the only shared surface.

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
- `GET /{name}/read` and `POST /{name}/write` — the only API agents need
- `POST /{name}/append` for non-destructive multi-agent writes
- sync.py conflict resolution (high version wins)
