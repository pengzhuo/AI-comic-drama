# Repository Map (agent navigation)

Compact index for coding agents. Goal: find the right file fast **without** loading
whole files. Prefer `grep` / `glob` and `Read` with `offset`+`limit`; for multi-file
questions dispatch an `explore` subagent instead of reading a tree.

## Never read wholesale

These files are far larger than any useful context window. Always `grep` them, or read
a narrow `offset`/`limit` slice. Do not `Read` them end-to-end.

| File | ~Size |
| --- | --- |
| `frontend/src/features/superchat/superchat-panel.tsx` | 692 KB |
| `src/novelvideo/api/routes/freezone.py` | 688 KB |
| `src/novelvideo/generators/nanobanana_grid.py` | 320 KB |
| `src/novelvideo/chat/service.py` | 238 KB |
| `src/novelvideo/api/routes/generation.py` | 238 KB |

(Long-term work order: split these — see the context-overflow plan.)

**Split so far (M3):**
- `generators/prompt_builder.py` → facade; library in `generators/prompt_components.py`, strategies in `generators/prompt_strategies.py` (all re-exported).
- `api/routes/generation.py` → helpers moved to `api/routes/generation_helpers.py`; the route module re-imports them. Helpers reference the source module dynamically (`_gen.<name>`) so monkeypatching keeps working.
- `chat/service.py` → a block of helpers moved to `chat/service_helpers.py` (dynamic `_svc.<name>` refs; re-imported).
- `api/routes/freezone.py` → assets/init routes moved to `api/routes/freezone_assets.py` (dynamic `_fz.<name>` refs; the decorators register on the main router; handlers re-exported).

Still to split: `frontend/src/features/superchat/superchat-panel.tsx` (main
component at ~line 12521; first ~12.5k lines are helpers/subcomponents). It is a
strict-TS file (`noUnusedLocals`), so a mechanical move needs a TS-aware refactor
with iterative `tsc -b` rather than a blind cut.

## Backend entry points (`src/novelvideo/`)

| Path | Responsibility |
| --- | --- |
| `cli.py` | CLI entry: `novelvideo api --port 8780` |
| `api/app.py` | FastAPI app construction, startup hooks |
| `api/__init__.py` | Router registration (`api_router`, `/api/v1`) |
| `api/routes/*.py` | REST routes: `ingest`, `projects`, `characters`, `episodes`, `scripts`, `content`, `generation`, `tasks`, `pipeline`, `freezone`, `chat`, `styles`, `assets`, `scenes`, `props` |
| `api/deps.py` | Project scope/store factories (`resolve_project_scope`, `make_sqlite_store_for_context`) |
| `api/schemas.py` | Pydantic request/response models |
| `task_backend/registry.py` | Task runner registry |
| `task_backend/client.py` | `enqueue_project_task` / `cancel_project_task` facade |
| `task_backend/runners/__init__.py` | Registers all built-in runners |
| `task_backend/runners/*.py` | Per-stage runners (`ingest`, `graph_build`, `identity`, `script`, `sketch`, `render`, `video`, `audio`, `character_image`, `episode_assets`, `freezone`) |
| `ports/local/tasks.py` | CE in-process task backend (`InlineTaskBackend`) |
| `chat/service.py` | Chat/agent orchestration + streaming |
| `chat/director_auto.py` | Durable server-side auto coordinator (episode / project scope) |
| `chat/dramaclaw_mcp.py` | MCP server (see `.mcp.json`) |
| `agents/*.py` | LLM agents: `episode_planner`, `identity_planner`, `content_rewriter`, `asset_compiler`, `global_video_optimizer` |
| `workflows/*.py` | Script-writing workflows (`script_writing`, `literal_script_writing`) |
| `structured_ingest.py`, `structured_extraction.py`, `structured_builders.py` | Structured (`structured_v1`) ingest + extraction |
| `cognee/` | Legacy knowledge-graph pipeline (`store`, `pipeline`, `event_extractor`, `screenplay_normalizer`) |
| `generators/` | Media generation (`video_composer`, `prompt_builder`, `nanobanana_grid`, image/video generators) |
| `seedance2_i2v/` | Seedance image-to-video pipeline |
| `freezone/` | XiaHua canvas: `workflow_runs`, `agent_workflows/`, `agent_catalog/`, `text_node`/`image_node`/`video_node`, `recipe_runtime` |
| `director_world/` | 3D previz/staging/voxel + spatial contracts |
| `export/` | Episode export (SRT/video/zip) |
| `verification/` | Quality gates |
| `styles/`, `prompts/`, `skills/`, `assets/` | Bundled content |
| `utils/` | `model_context_budget`, `document_parsers`, `path_resolver`, `screenplay_scene_parser`, `upload_safety` |
| `shared/runtime_env.py` | Runtime/edition flags (`is_ce_effective`) |

## Frontend (`frontend/src/`)

| Path | Responsibility |
| --- | --- |
| `routes/` | TanStack Router routes (`_app/projects.$project/*`) |
| `features/superchat/` | AI assistant panel + director run modes (`director-run-mode.ts`, `superchat-panel.tsx`) |
| `features/canvas/` | XiaHua node canvas (nodes, compose, ui) |
| `features/freezone/` | Freezone shell, canvas host, sync |
| `features/canvas/nodes/`, `features/viewer-kit/` | Node editors, 3D/pano viewers |
| `task-center/` | Task Center store/provider/stream |
| `lib/queries/` | Typed API query helpers |
| `components/`, `stores/`, `hooks/` | Shared UI, state, hooks |
| `index.css` | CSS variables — mirror `DESIGN.md` |

## Tests

- `tests/` — default suite; `tests/contract/` API contracts; `tests/ports/` port-focused.
- `uv run pytest`, focused: `uv run pytest tests/test_director_auto.py`.
- Frontend: `cd frontend && npx tsc -b` and `npx vitest run <path>`.

## Navigation tips

- Locate symbols with `grep` (`Select-String` on Windows) before reading; then `Read` a window.
- Prefer `explore` subagents for "where is X / how does Y work" across many files.
- For a single known file, `Read` with `offset`/`limit` around the `grep` hit.
- Keep `git diff`/`git status` scoped to the files you touched.
