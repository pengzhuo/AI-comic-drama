---
name: superchat-frontend
description: Use when working on the AI assistant / director UI under frontend/src/features/superchat (superchat-panel.tsx, director-run-mode.ts, use-superchat.ts, message.ts) — assistant message transport, run modes, task banners. Front-load these keywords: superchat, director-run-mode, superchat-panel, use-superchat.
---

# superchat frontend

Scope: the web AI-assistant (虾导/捏导) surface.

## Hard rule

`frontend/src/features/superchat/superchat-panel.tsx` is ~690 KB. **Never read it end-to-end.**
Use `grep` to find the symbol, then `Read` a narrow `offset`/`limit` window around the hit.

## Where things live

| File | Responsibility |
| --- | --- |
| `superchat-panel.tsx` | Main panel: composer, message send pipeline, run-mode dropdown, task banners, media cards |
| `director-run-mode.ts` | Run modes (`manual_confirm` / `episode_auto` / `pipeline_auto`), transport-text builders, task decisions |
| `director-run-mode.test.ts` | Vitest unit tests for the above |
| `use-superchat.ts` | Chat state hook, SSE/event wiring, `director-auto-status` |
| `message.ts` | Message model / rendering helpers |
| `chat-task-status-bar.tsx`, `composer-waiting-status.tsx` | Status UI |
| `task-notification-batch.ts` | Batches task notifications |

Backend counterpart: `src/novelvideo/chat/director_auto.py` and `src/novelvideo/api/routes/chat.py`
(the `chat/director-auto` endpoints). Transport markers such as `[DRAMACLAW_RUN_MODE]` and
`mode=pipeline_auto` must stay in sync between `director-run-mode.ts` and the backend prompt.

## Working method

1. `grep` for the symbol or marker first (e.g. `Select-String -Path frontend\src\features\superchat\*.ts* -Pattern "..."`).
2. Read only the surrounding window.
3. Change the smallest surface; keep the existing conventions (props, hooks, i18n keys via `t(...)`).
4. Verify: `cd frontend && npx tsc -b` and `npx vitest run src/features/superchat`.

## Visual changes

Read `DESIGN.md` first; it is the source of truth and mirrors the CSS variables in
`frontend/src/index.css`. Keep `npx @google/design.md lint DESIGN.md` at 0 errors.
