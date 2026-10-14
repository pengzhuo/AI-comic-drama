# Hermes Sandbox & the CE Unsandboxed Fallback

Hermes workers run inside an OS sandbox (`sandbox_wrap.py`): macOS uses the system
`sandbox-exec`; Linux uses `codex-linux-sandbox`, whose default pipeline execs
**bubblewrap** (`bwrap`) — Landlock is only its `--use-legacy-landlock` fallback, which
we do not use. When the sandbox cannot be used the wrapper **fails closed** on
EE/production and **degrades with a loud warning** on single-tenant CE (see below).

How the Linux sandbox ships (the P1① half of **#346**, now done):

- The vendored binaries live at `deploy/sandbox/linux-{amd64,arm64}/codex-linux-sandbox`
  and reach the image via `COPY deploy ./deploy`. The `Dockerfile` then **installs the one
  matching `TARGETARCH` onto `/usr/local/bin`** (where `_wrap_linux` looks it up) and
  installs the `bubblewrap` package. A build-time `--help` smoke proves the ELF loads.
- `codex-linux-sandbox` still needs a **host kernel with unprivileged user namespaces** at
  runtime for `bwrap`; installing the binary does not create that capability.
- **Linux wrapping is off by default** (`SUPERTALE_LINUX_SANDBOX` unset ⇒ `_wrap_linux` routes
  through `_fallback_or_raise` without wrapping). Reason: codex's managed `restricted` profile is
  **allow-only** and its narrowest read grant is the broad `root` special — so a wrapped Hermes on
  a shared host can **read peers' `state/output/runtime` data** (write is denied; read is not).
  Narrowing the read scope in-profile empirically breaks the `bwrap` re-exec entirely, so peer-read
  confidentiality is **not** closable at the profile layer — it must be closed at the **deployment
  layer** by mounting only the current user's slice into the container. `SUPERTALE_LINUX_SANDBOX=1`
  is the deployment's explicit assertion that it has done so; set it **only** there. Until then EE
  fail-closes (won't run with false read-isolation) and single-tenant CE degrades/refuses per its
  opt-in (single tenant ⇒ no cross-user read risk). Per-user mounts + a test asserting peer-read
  *fails* are #346 P1②.
- **Network:** the Linux profile allows **outbound** (`"network": "enabled"`), matching the macOS
  Seatbelt profile's `(allow network-outbound)`. codex's `"restricted"` mode `--unshare-net`s the
  sandbox — *no* egress — which strangles Hermes's required calls to the project API
  (`DRAMACLAW_API_URL`) and the model gateway, and the `/bin/true` probe cannot see that break.
  Tightening egress to an allowlist is P1② below.

Two enforcement layers, both driven by the same `_fallback_or_raise` decision:

- **Runtime, per worker** — `_wrap_linux` runs a one-time cached probe (`_sandbox_can_run`:
  `/bin/true` inside a throwaway sandbox). A *missing binary* **or** a *present-but-unusable*
  sandbox (kernel lacks user namespaces) both route through `_fallback_or_raise`.
- **Boot, per container** — `deploy/docker-entrypoint.sh` runs the startup gate
  `deploy/hermes_sandbox_selfcheck.py` before exec-ing the API, **but only when the selected chat
  backend is Hermes** (it resolves `DRAMACLAW_CHAT_BACKEND` → `SUPERTALE_CHAT_BACKEND` → default
  `hermes`, mirroring `chat.service._chat_backend`). A codex/claude backend, migrations, diagnostics,
  or any command overriding the CMD skip the gate — the sandbox is a Hermes-worker constraint, not a
  whole-image startup condition, so it must not fail-close unrelated backends (`_wrap_linux` still
  fail-closes the Hermes worker itself). When it does run, its exit code decides whether the container
  boots at all. Every degrade path in the gate is guarded by `_may_degrade()`
  (`not _sandbox_required()` **and** `SUPERTALE_ALLOW_UNSANDBOXED` set; import-failure ⇒ never
  degrade) — the exact CE-single-tenant-opt-in condition `_fallback_or_raise` uses, so the gate
  can never boot a config the wrapper would have refused. CE without the opt-in **refuses** on
  every unusable-sandbox path, not just on EE.

The decision in both places:

- **EE / production** (`SUPERTALE_ENV=production` or `ST_CONTROL_PLANE_DSN` non-empty) — sandbox
  unusable ⇒ **refuse** (raise at runtime; entrypoint refuses to boot). The `SUPERTALE_ALLOW_UNSANDBOXED`
  flag cannot override this.
- **Single-tenant CE** (DSN empty) with `SUPERTALE_ALLOW_UNSANDBOXED=1` — sandbox unusable ⇒
  **degrade**: loud warning, run Hermes unsandboxed rather than lock a self-hoster out on an old
  kernel. Single-tenant has no cross-user data-isolation risk. The four CE compose files set this
  flag as that **degrade valve** (not a blanket "always unsandboxed" — when the sandbox works, it
  is used). CE without the flag still refuses.

`tests/test_compose_ce_unsandboxed_gate.py` and `tests/test_sandbox_linux_probe.py` pin both the
compose contract and the runtime/boot behavior. Do not remove the compose opt-in unless you accept
that CE will fail closed on kernels without unprivileged user namespaces.

Still open in **#346** (P1②):

1. **Linux peer-read isolation** — mount only the current user's `state/output/runtime` slice into
   the Hermes container (the profile layer cannot close this; see above), then flip
   `SUPERTALE_LINUX_SANDBOX=1` in that deployment and add a test asserting a wrapped process
   **cannot** read a peer's data (today `tests/sandbox_linux_isolation.py` only *reports* peer reads;
   it must assert they fail).
2. **Egress allowlist** — both platforms currently allow **all** outbound. Tighten to a controlled
   allowlist (project API + model gateway only) — e.g. codex's `--allow-network-for-proxy` + a proxy
   route spec on Linux, and the matching Seatbelt narrowing on macOS — plus a test that a minimal
   Hermes API/model call actually completes from inside a real Linux sandbox.
