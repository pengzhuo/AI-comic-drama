---
name: freezone-backend
description: Use when working on the Freezone / XiaHua canvas backend (src/novelvideo/freezone, src/novelvideo/api/routes/freezone.py) — workflow runs, agent workflow catalog/recipes, canvas nodes, canvas command bridge. Front-load these keywords: freezone, workflow_runs, canvas_command_bridge, agent_workflows.
---

# Freezone (XiaHua canvas) backend

Scope: the node-canvas backend and its agent workflows.

## Hard rule

`src/novelvideo/api/routes/freezone.py` is ~688 KB. **Never read it end-to-end.**
`grep` for the route/function name, then `Read` a narrow `offset`/`limit` window.

## Where things live (`src/novelvideo/freezone/`)

| File | Responsibility |
| --- | --- |
| `workflow_runs.py` | Canvas workflow-run lifecycle (create/update/reconcile) |
| `agent_workflows/` | `catalog.py`, `graph.py`, `drafts.py`, `registry.py` — plan/draft/catalog |
| `agent_catalog/builtins/` | Bundled skills + recipes (JSON) |
| `canvas_command_bridge.py` | Durable inbox/outbox for browser-executed canvas commands |
| `text_node.py`, `image_node.py`, `video_node.py` | Node-level generation |
| `recipe_runtime.py` | Recipe compile/execute (`_limit_model_context`) |
| `workflow_schema.py`, `workflow_transaction*`, `workflow_observation.py` | Schema and transactions |
| `history.py`, `skill_import*.py`, `presets.py`, `jobs.py` | History, skill import, presets, async jobs |

Routes: `src/novelvideo/api/routes/freezone.py` (all `/freezone/*` endpoints).
Task runner: `src/novelvideo/task_backend/runners/freezone.py`.

Agent-facing tools live in `.hermes/plugins/freezone/`; the tool-name policy is in
`src/novelvideo/chat/tool_policy.py`.

## Working method

1. `grep` the route path or function name in `api/routes/freezone.py` before reading.
2. For behavior questions spanning modules, dispatch the `explore-py` subagent.
3. Keep route handlers thin; put logic in the `freezone/` modules.
4. Verify: `uv run pytest tests/ -k freezone -q`.

## Notes

- Canvas commands are executed by the browser; the backend persists them in SQLite
  (`canvas_command_bridge.py`) — do not assume headless execution.
- `src/novelvideo/freezone/canvas_command_bridge.py` imports `fcntl` (Unix-only); on
  Windows dev boxes provide a shim on `PYTHONPATH` when running API tests.
