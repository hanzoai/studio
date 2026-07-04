# Hanzo Studio

## Overview
**The most powerful and modular visual AI engine and application.**

## Tech Stack
- **Language**: Python

## Build & Run
```bash
uv sync
uv run pytest
```

## Structure
```
studio/
  CODEOWNERS
  CONTRIBUTING.md
  Dockerfile
  LICENSE
  QUANTIZATION.md
  README.md
  alembic.ini
  alembic_db/
  api_server/
  app/
  blueprints/
  branding/
  comfyui.log
  comfyui.prev.log
  comfyui.prev2.log
```

## Key Files
- `README.md` -- Project documentation
- `pyproject.toml` -- Python project config
- `Dockerfile` -- Container build (applies `branding/` then runs `main.py`)
- `hanzo.yml` + `.github/workflows/cicd.yml` -- canonical CI/CD (hanzoai/ci); builds `ghcr.io/hanzoai/studio`, rolls the `studio` operator Service CR at `studio.hanzo.ai`.

## Custom-node packs ("hanzo packages")
The popular ComfyUI packs are vendored as first-class hanzo packages, pinned and
reproducible. One and only one way to add/bump them:

- **Manifest**: `custom_nodes/hanzo-packs.txt` — `<dir> <git_url> <commit_sha>
  <spdx>` lines (immutable SHAs). This is the tracked source of truth; the cloned
  pack sources are gitignored (vendored at build).
- **Deps**: `custom_nodes-requirements.txt` — hand-resolved, only what's not
  already in `requirements.txt`. Installed with the studio torch(cu130)/NumPy 2.x/
  Transformers 5.x stack **locked** so packs can never downgrade it.
- **Installer**: `scripts/install_custom_nodes.sh` clones each pack at its pin and
  installs deps; the lock is derived from the live env (correct for both the CPU
  image and the GB10 cu130 venv). `ultralytics` is installed `--no-deps` (else it
  pulls non-headless opencv). Local (GB10): `STUDIO_PY=.venv/bin/python
  STUDIO_PIP="uv pip install --python .venv/bin/python" scripts/install_custom_nodes.sh`.
- **Image**: `Dockerfile` runs the installer after `COPY . .`; `.dockerignore`
  re-includes only the manifest + installer (`custom_nodes/*` / `scripts/*` form).
  So `git push origin main` → CI builds `ghcr.io/hanzoai/studio` with the packs →
  studio.hanzo.ai gets them on the next image build.

**Compat shim — why packs work here at all.** This fork renamed ComfyUI's core
packages (`comfy`→`studio`, `comfy_extras`→`studio_extras`, `comfy/comfy_types`→
`studio/node_types`, and the `Comfy*`→`Studio*` API classes; see
`scripts/rename_modules.py`). `studio_compat/` is one meta-path finder that lazily
aliases every `comfy*` import to its `studio*` counterpart and mirrors
`ComfyExtension`/`io.ComfyNode`→`Studio*` — so upstream packs run **unmodified**,
no forks/renames. It is the phase-4 redirect `rename_modules.py` deferred, done
with the 3.12 `find_spec` API. Activated by `import studio_compat` at the top of
`main.py`. `nodes.py` also honors the upstream `comfy_entrypoint` V3 name.

Restart the local worker only when idle: `scripts/studio-service-swap.sh` polls
`/queue` and restarts on the first empty window. Bad packs are isolated by the
loader (studio still boots); genuinely-incompatible ones go to
`custom_nodes_quarantine/` (e.g. was-node-suite: hard `numba` dep vs NumPy 2.5).

## Multi-tenant + IAM (cloud: studio.hanzo.ai)
Standalone app; `console.hanzo.ai` link-outs land here already authenticated via
shared Hanzo IAM (standard OIDC code flow). No embedding.

- **Auth** (`middleware/iam_auth_middleware.py`): local **JWKS** JWT validation
  against `${STUDIO_IAM_URL}/v1/iam/.well-known/jwks` — signature + `exp` +
  `iss` + `aud` (client_id `hanzo-studio`). Org from the `owner` claim
  (`organization` fallback) → `request["iam_user"]["org_id"]`. Anonymous mode:
  off by default locally (loopback bypass); enable in cloud with
  `STUDIO_ENABLE_IAM_AUTH=true` (the one auth switch — this is the
  "STUDIO_AUTH=off default" behavior). Browser flow: unauth HTML → IAM
  `authorize` (code+PKCE) → `/callback` exchanges the code, sets an httpOnly
  session cookie (no frontend changes). `verify_jwt(...)` is a pure, unit-tested
  function.
- **Tenancy** (`folder_paths.py`, `app/user_manager.py`): with
  `STUDIO_MULTI_TENANT=true`, userdata is `user/orgs/{org}/user/{user}/…` and
  outputs are `output/orgs/{org}/…`. `get_public_user_directory(user, org)` is
  org-rooted (fixed the bug where org userdata always resolved to `None`).
  Execution outputs are scoped via `folder_paths.set_execution_org()`, bound by
  the prompt worker from the queue item's `org_id`.
- **GPU federation** (`middleware/{worker_client,prompt_router,compute_config,
  visor_client}.py`): `/v1/workers/register` (heartbeat), `/v1/workers` (list),
  `/v1/worker/execute` (in-cluster push fast path). Worker↔coordinator trust via
  `X-Worker-Token` (`STUDIO_WORKER_TOKEN`, KMS-sourced).
- **Durable render queue** (`middleware/tasks_queue.py`, `docs/federation.md`):
  `SqlitePromptQueue` — crash-durable render queue in a single SQLite file
  (stdlib `sqlite3`, WAL, **zero external processes**). `STUDIO_QUEUE_DB=<path>`
  → `server.py` swaps `PromptQueue` for it (precedence **STUDIO_QUEUE_DB >
  STUDIO_PERSIST_QUEUE > memory**; default local behavior unchanged). The
  `PromptQueue` seam maps to a durable job state machine: `put→INSERT (pending,
  idempotent on prompt_id)`, `get→claim next pending under BEGIN IMMEDIATE
  (exactly-once, leased)`, `task_done→done | retry/failed`. Crash-recovery: a
  lease + reap returns an abandoned claim to `pending` (on every get(), a
  heartbeat thread, and on boot) so another process re-runs it — idempotent
  (SaveImage suffixes increment). Multiple Studio processes on the same DB file
  compete safely. Tested: `middleware_test/tasks_queue_test.py` (incl.
  crash-recovery across instances). Future (federation.md §6): a remote queue
  backend served by Hanzo Tasks + a Go/unified-binary (HIP-0106) migration.
- **Engine selector** (`middleware/engine_selector.py`): `/v1/engines` lists
  execution targets for the org (`local` + registered compute_config workers) and
  `PUT /v1/engines/default` sets a per-org default (stored on
  `ComputeProfile.default_engine`). `prompt_router.route_prompt` routes to the
  chosen worker; `local` = unchanged. Leased cloud machines (Visor
  `GET /v1/machines`) are a future engine class (interface stubbed, not built).
  Frontend picker is a TODO. Tested: `middleware_test/engine_selector_test.py`.
- **Chat Copilot** (`middleware/copilot.py` + `branding/hanzo-copilot.{js,css}`):
  a sidebar chat that reads and MUTATES the workflow graph in natural language.
  Backend `POST /v1/copilot/chat` proxies one OpenAI tool-calling round to a
  chat endpoint (`STUDIO_COPILOT_URL`; default = local engine
  `127.0.0.1:1234` if its port is open, else `api.hanzo.ai`; `STUDIO_COPILOT_MODEL`
  default `zen`; token from `STUDIO_COPILOT_TOKEN`→`STUDIO_COMMERCE_TOKEN`→request
  bearer). It returns `{reply, ops}`; `add_node` ops are validated against
  `nodes.NODE_CLASS_MAPPINGS` server-side so the model can't invent node types.
  The **frontend** applies ops against `app.graph` (LiteGraph) — op schema:
  `add_node`(id?/pos?) · `set_widget` · `connect`(slots by name or index) ·
  `set_prompt` · `move_node` · `delete_node` · `layout` (columnar tidy) ·
  `queue`. Every op is guarded (unknown type/slot = skip + note, never throw).
  Injected into the prebuilt frontend by `branding/patch_copilot.py` (copies the
  bundle into static/, CSS `<link>` in `<head>`, deferred `<script>` before
  `</body>`; idempotent, wired as step 9 of `apply-branding.sh`) — survives
  frontend upgrades. `window.HanzoCopilot.applyOps` is exposed for tests. Tab
  registered via `app.extensionManager.registerSidebarTab` (id `hanzo-copilot`).
  Tested: `middleware_test/copilot_test.py` (op-validation + mocked tool loop) and
  `branding/copilot_smoke.py` (headless: applier mutates a mock graph + panel
  renders). Live end-to-end needs one server restart to register the route.
- **Identity user menu + session API** (`middleware/session.py` +
  `branding/hanzo-identity.{js,css}`): the top-right user menu, driven entirely
  by the IAM-validated token. Backend (thin, `/v1` house style, registered in
  `add_routes` so it works auth-on and auth-off): `GET /v1/session` →
  `{user,email,org,orgs[],project,projects[],console_url,iam_url,authenticated}`
  read from `request["iam_user"]`; `POST /v1/session/org` sets the active org
  (validated against membership → `studio_active_org` cookie); `POST
  /v1/session/project` sets the workflow-workspace project scope; `GET|POST
  /logout` clears the session + selection cookies and bounces to the IAM
  end-session → `/login`. **Org switching is real, one hook**: the auth
  middleware applies `session.resolve_org(...)` to a COPY of the validated user
  (never the cached dict) so `iam_user["org_id"]` — the id every tenancy/output
  path already reads — reflects the switch; membership comes from
  `_orgs_from_claims` (`owner` + `organizations`/`orgs`/`groups`, list or CSV or
  `{name}` dicts, home first, deduped). Frontend is a SPA-agnostic fixed pill +
  dropdown (avatar/initials, user, org switcher, project switcher, Console
  link → `console.hanzo.ai`, Logout); 401 → "Sign in with Hanzo" → `/login`.
  Injected by `branding/patch_identity.py` (step 10 of `apply-branding.sh`,
  same seam as the copilot). `window.HanzoIdentity` is exposed for tests.
  Tested: `middleware_test/session_test.py` (resolve/orgs/routes/logout, real
  aiohttp app) + `scratchpad/studio-iam/test_identity.py` (headless: pill,
  dropdown, org switch re-scope, signed-out). The `/v1/session` routes need one
  server restart to register (static + de-Comfy load without restart).
- **De-Comfy display strings** (`branding/patch_destring.py`, step 11 —
  runs LAST): the systematic English brand-word sweep the curated phrase list in
  `patch_frontend.py` structurally can't reach. Rule 1: whole-word `ComfyUI` →
  `Hanzo Studio` only where it reads as a display word (preceded by
  open-quote/space/`>`/`(`, followed by close-quote/space/sentence-punct) — that
  boundary skips every code use in the bundle (`ComfyUIStarted`, `.ComfyUI`,
  `=ComfyUI`, the `/^(ComfyUI-.../` regex, dotted i18n keys `.category.ComfyUI`)
  and hyphenated product names (`ComfyUI-Manager`). Rule 2: curated bare-`Comfy`
  **values** as exact `key:` value`` pairs so the Settings dialog **keys**
  (`Comfy`, `Comfy-Desktop`, `Comfy.ColorPalette`) are preserved while the
  displayed section header becomes **Studio** / **Studio Desktop** (this is the
  "Settings must not say Comfy" fix) + `Comfy Cloud` → `Hanzo Cloud`. Scope:
  English only — files above `LOCALE_RATIO` (0.15%) non-ASCII are skipped (all 33
  locale bundles are ≥0.31%; English bundles ~0.02%) and each match needs a
  pure-ASCII window, so the non-English `main-*/nodeDefs-*/commands-*` chunks are
  left as-is (noted, out of scope). Idempotent. Verified: served English display
  `ComfyUI` 63→0; a few example filesystem paths (`Documents/ComfyUI`) and the
  external comfy.org service labels (`Comfy API Key`, `Comfy Node Registry`,
  behind disabled API-nodes) are intentionally left. patch_frontend.py keeps its
  orthogonal concern (URLs/domains + `Comfy-Org` → `Hanzo AI`).

## Tests
`tests-unit/{folder_paths_test,middleware_test,app_test,prompt_server_test}` —
run `uv run pytest tests-unit/folder_paths_test tests-unit/middleware_test
tests-unit/app_test tests-unit/prompt_server_test -q`. The auth/tenancy work is
covered by `middleware_test/iam_auth_test.py`,
`middleware_test/session_test.py` and
`folder_paths_test/org_scoping_test.py`.
