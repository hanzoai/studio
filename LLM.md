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
- **Per-GPU gpu-jobs queue** (`middleware/{gpu_dispatch,studio_home}.py`, `server.py`) —
  the ONE submit path onto a GPU. A model-less coordinator's `POST /prompt` enqueues to
  the cloud tasks engine (`POST {STUDIO_CLOUD_API_URL}/v1/tasks/namespaces/gpu-jobs/activities`,
  user bearer → org-scoped); a BYO worker (`hanzo gpu connect`) claims + runs it.
  **Targeting**: the Home "Run on <gpu>" chip sets `X-Target-GPU: <identity>`;
  `dispatch_if_worker` enqueues on that machine's own lane `taskQueue="gpu:<identity>"`
  (namespace stays `gpu-jobs`), else the shared `gpu-jobs` lane any worker drains —
  `X-Target-GPU` rides through `_queue_prompt` so fix/compose/video/rerun all honor it.
  **Authoritative visible queue**: `/v1/render-queue` + `/v1/nodes` read the tasks
  engine's activity list — REAL claiming `identity`, status
  SCHEDULED/STARTED/COMPLETED/FAILED → queued/running/done/failed, `lastHeartbeatTime`
  liveness, per-node depth — not a local shadow; `render_jobs.json` survives only as a
  ~2s pre-claim placeholder. **No hidden runs**: in `--worker-mode` a direct `/prompt`
  POST is refused 403 unless it carries `X-Worker-Token` — the only accepted execution is
  a claimed job handed in over `POST /v1/worker/execute`
  (`worker_client.reject_untrusted_worker_submit`, one policy gate). Legacy push
  (`prompt_router`) is OFF unless `STUDIO_LEGACY_PUSH_ROUTER=1`. Tests:
  `middleware_test/{gpu_dispatch_test,worker_seam_test}.py`.
- **Edit strength follows the TRANSFORMATION DISTANCE, not the look** (`studio_home.py` `FLOWS`):
  a lane declares `denoise` ONLY when it is a conversion. `cad_ghost`/`cad_mockup`
  (drawing→photograph) run 0.92 because a large change is required; `product`/`lifestyle`
  name an output LOOK, so they declare nothing and inherit `EDIT_DENOISE_DEFAULT` 0.25 —
  the print, the model's identity, the framing and the resolution survive. `outfit`
  (garment swap) is the one non-conversion lane that replaces a large region: 0.45.
  Braiding the two shipped `product` at 0.90 and a "remove the drawn lines" edit came back
  with the watercolor print replaced by plaid and a different model. Tested both ways
  (`flows_test.py`): no style lane above 0.5, no conversion lane below 0.85.
- **Retry failed work — the ONE replay path** (`studio_home.py` `retry_row` +
  `POST /v1/worklog/retry`): `{ids:[…]}` or `{failed:true,limit:N}` re-enters the SAME
  `dispatch_*` funnel a fresh submit uses, so a replay picks up every current rule with no
  replay-only code. The operation comes from the recorded **output prefix** (`kind` carries
  the FLOW, so it cannot name the op); the flow from `kind`. A row whose sources were never
  recorded is REFUSED with the reason, never approximated; originals stay in history. The UI
  (`↻ Retry` per failed row, `↻ Retry failed` in Your work) sends **chunks of 3** — each
  retry is a full dispatch, so one request for 17 never returns.
- **Engine verdicts reach the worklog** (`_fold_engine`): an engine-failed/cancelled job
  leaves no landed output, so `_finalize_landed` can never close it — without the fold it
  read `queued` forever. `done` is still ONLY the integrity gate's verdict.
- **Output integrity — done means 100% complete** (`studio_home.py`): `_intact()` reads
  the format's terminal marker (PNG IEND / JPEG EOI / RIFF size / a consistent MP4 box
  chain) — encoders write it LAST, so presence proves the write finished. In
  `_finalize_landed` a truncated landing reads **failed** (never done), a file inside the
  `_SETTLE_SECS` window with no proof is judged on a later pass, and `_index_upload`
  refuses a truncated file at the gallery's one ingest point. Sits beside the
  implausibly-fast honesty guard (`_IMPLAUSIBLE_SECS` — an engine that didn't sample
  never delivers). Tests: `middleware_test/flows_test.py`.
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
- **Stacks** (`middleware/stacks_store.py`, `middleware/studio_home.py`): Procreate-
  style gallery folders — a per-org SQLite store (`orgs/<org>/stacks.db`, WAL, every
  mutation under `BEGIN IMMEDIATE`, the same `tasks_queue.py` pattern) that is ONLY an
  organizational layer; assets stay the source of truth (library.json + files). Schema
  = the spec's `stacks` + `stack_assets` join; `UNIQUE(asset_id)` ⇒ one Stack per
  asset. Tenancy is structural (one DB file per org → cross-org 404s by construction).
  REST (`/v1` house style): `GET/POST /v1/stacks`, `GET/PATCH/DELETE /v1/stacks/:id`
  (`?mode=only|images`), `POST|DELETE /v1/stacks/:id/assets`, `PATCH /v1/stacks/:id/order`,
  `POST /v1/stacks/:id/unstack`, `GET/PATCH /v1/stacks/settings`; registered via
  injected resolvers (org/owner/workspace + the library soft-deleter) so the module
  stays decoupled. Remove ≠ delete (returns to the gallery); Delete-Stack defaults to
  preserving; "…and Images" maps to the library soft-delete (`status=deleted`, files
  kept). Generation integration: `fix/compose/rerun/template` accept a `stack` id and
  the ingest point (`_index_upload` → `stacks_store.absorb`) assigns membership when
  outputs land (ALL outputs of a multi-output job); a per-org `auto_stack_batches`
  setting auto-creates `"<prompt> — <date>"` Stacks. UI is the vanilla-JS home
  (`studio_home.html`): layered cards, drag/multi-select/mobile-tap parity (no DnD ever
  required), a11y labels + live region. Tested: `studio_home_test/test_stacks.py`
  (routes, tenancy, one-per-asset, order persistence, delete/unstack, ingest→absorb).
- **Library QC gallery — provenance + Publish to CMS** (`middleware/{studio_home.py,
  studio_home.html,content_publish.py}`): the Assets tab IS the QC gallery (grid `#agrid`
  fed by the ONE `GET /v1/library`, filter chips + search + bulk bar + full-size viewer);
  this closes its two gaps without adding a surface.
  **Provenance is a JOIN, never a new store.** Both records already existed — the API
  graph the saver embeds in the output, and the dispatch row in `worklog.json`. They are
  now joined: `_producer(rows, rel)` (the row whose landed `output` IS this asset) is
  returned as `lineage.job` by `GET /v1/worklog/lineage`, and `openLineage()` hydrates
  `CTX` from it, so a gallery item's context panel shows the same prompt · references ·
  versions · process trail as a queue item and **Open in Editor** works for it.
  `_provenance()` is no longer a second graph distiller: it is `_graph_params()` (the one
  the dispatch record uses) plus what only a finished artifact carries — the saver's
  prefix, `content_publish.model_of()` and the graph size — rendered as a **Provenance**
  block linking `/v1/workflow?id=`.
  **`POST /v1/library/publish`** pushes ONE asset into the Hanzo content lane
  (cms.hanzo.ai = framework DocTypes; `POST {STUDIO_CLOUD_API_URL}/v1/framework/Asset`)
  as a **draft** `Asset` carrying its graph — `content_publish`, the one writer, on demand
  instead of only at render completion (`main.py`). Distinct from `POST /v1/library/status`
  `published`, which is this workspace's OWN curation state — different systems, so a
  different route and the distinct label **☁ Publish to CMS** (card · bulk bar · viewer).
  `publish_render()` returns `{landed, errors:[{status, message, path}]}` (still never
  raises) and the route returns the REAL upstream status: **403** (`FORBIDDEN_REMEDY`
  appended), 401 signed-out, 503 switched off, 404 unknown/other-tenant asset. The UI
  shows failures verbatim in a dialog that STAYS UP (`#pubdlg`), never a toast.
  **Known cloud-side blocker**: the lane refuses `create on Asset` unless the caller holds
  **Content Editor** (or SuperAdmin/System Manager) — cloud `clients/content` `contentPerms()`.
  Studio cannot fix it (the Asset is written as the caller's own IAM principal, by design —
  no service account, no cross-org write path); an org owner grants the role via
  `POST /v1/framework/roles`. There is **no commerce publish API** to wire: the content
  lane's commerce link is a `product` handle on `Campaign`, and `billing_middleware` only
  meters usage — so the action is scoped to the Asset lane rather than inventing one.
  Tested: `studio_home_test/publish_test.py` (route success/403-propagation/401/503/404 +
  tenant isolation on a real aiohttp app, the lineage join, the one distiller, and the
  served-HTML invariants) and `middleware_test/content_publish_test.py`.
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

## Office documents (the non-GPU pipeline)
Studio's creative side is ComfyUI graphs → GPU. Office documents are the **orthogonal**
subsystem: structured content (a template's typed fields, filled by a person OR by the AI)
→ a pure-Python renderer → a real `.pdf/.docx/.xlsx/.pptx/.md`. No graph, no model, no GPU.
NOT bolted onto graphs; its own module, store, endpoints, and Home tab.

- **Renderers** (`middleware/doc_render.py`, pure — no HTTP/storage/org): one neutral IR
  per archetype (`doc` prose blocks · `sheet` sheets · `deck` slides) and EXACTLY ONE
  renderer per format. Adding a template = one `build(fields)->(kind,ir)`; adding a format =
  one renderer. Deps imported lazily so the module loads stdlib-only. **PDF** prefers
  WeasyPrint (full-CSS, monochrome print stylesheet); when its native pango/cairo stack is
  absent (the light CI unit venv, model-less pods) a **built-in pure-Python PDF writer**
  (base-14 Helvetica, WinAnsi/cp1252, auto-pagination, `/Info` title) produces a valid
  `%PDF` — so every format renders with just the wheels. **DOCX/XLSX/PPTX** = python-docx /
  openpyxl / python-pptx (real, always). Native libs in the `Dockerfile` (libpango/…).
- **Catalog + AI-fill + store + REST** (`middleware/documents.py`): ONE declarative `CATALOG`
  of 22 common templates (Documents 15 → pdf/docx/md · Spreadsheets 4 → xlsx/pdf/docx/md ·
  Presentations 3 → pptx/pdf/md). Each template's typed `fields` drive the form, the AI-fill
  JSON, AND the renderer inputs (one schema, three uses). Registered from within
  `add_studio_home_routes` with the SAME injected tenancy resolvers as `stacks_store`
  (`org_of`/`owner_of`). Per-org store `orgs/<org>/documents/` (atomic `index.json` +
  `<id>/doc.json`), doc.json is the source of truth, files rendered on demand at download.
  Endpoints (`/v1` house style): `GET /v1/documents` (grouped catalog),
  `GET /v1/documents/schema`, `POST /v1/documents` `{kind, ir, title?}`,
  `POST /v1/documents/generate` `{templateId, description?|fields?}`,
  `GET /v1/documents/library`, `GET|PATCH|DELETE /v1/documents/{id}`,
  `GET /v1/documents/{id}/download?format=`.
- **A stored document IS its `(kind, ir)`, and a template is a GENERATOR** — not a schema.
  An author (a person or an agent) `GET`s the document, edits the IR, and `PATCH`es
  `{ir}`, so a change is as precise as an edit to a file and is bounded by what the
  renderers can draw rather than by what some template's fields can say. `POST
  /v1/documents` skips the generator entirely: no template, no fields, no LLM. `fields`
  stay beside the IR as the form's own state, and `PATCH {fields}` REBUILDS from the
  template — the one way back to a generated document, and it discards an authored IR,
  which is what "regenerate" means. Two things fall out: a retired template no longer
  orphans a document (it carries its own IR; only a document written before this asks its
  template, and that read never writes back), and `GET /v1/documents/schema` publishes the
  grammar so an author is accurate instead of guessing — `doc_render.GRAMMAR` lives beside
  the renderers, and `test_schema_names_every_block_the_renderer_draws` walks the doc
  renderer so a block the grammar names and the renderer ignores cannot ship.
- **One proof, and it asks about the IR rather than about the pod** (`documents.prove`):
  every write renders once to `md`, the one format every archetype draws from the standard
  library. Rendering the primary format instead answered "this document is invalid" when
  the honest answer was "this pod has no python-pptx".
- **AI-fill is a LAYERED ENHANCEMENT** — the structured-form path renders a valid document
  with NO LLM, so a broken/unauthorized gateway can NEVER block creation (`422 needFields`
  → the UI reveals the prefilled form). It reuses the SAME working token the shipped Chat
  uses: the browser sends `window.HZ.pk` (the publishable `pk-` key `_publishable`/
  `_hz_config_script` emit) as the bearer to `/v1/documents/generate`; the backend forwards
  it, fail-closed, in order: (1) a **server-side gateway key** (`STUDIO_DOC_TOKEN`→
  `STUDIO_COPILOT_TOKEN`→`STUDIO_COMMERCE_TOKEN`); (2) else the caller's own token, forwarded
  ONLY when gateway-verifiable — a publishable `pk-` key or an asymmetric/JWKS JWT
  (RS/ES/PS/EdDSA). A symmetric **HS256 hanzo.id session token is NEVER forwarded** (the
  gateway rejects it: "unsupported signing method: HS256"), nor any secret `sk-`/`hk-` key.
  Model = `STUDIO_DOC_MODEL`→`STUDIO_CHAT_MODEL`→`enso` (matches window.HZ.model).
- **UI** (`middleware/studio_home.html`, vanilla JS, monochrome/Geist): a **📄 Documents**
  Home tab — searchable server-driven catalog + "Your documents" grid with Download
  PDF/Word/Excel/PowerPoint. The `docdlg` dialog: describe in one line → Generate with AI
  (sends window.HZ.pk), or fill the short typed form → Generate; edit fields before/after;
  re-open a saved doc to edit (PATCH). The dead `doc/sheet/paper` composer KIND placeholders
  were replaced with one "Office documents" card that opens the tab.

## Films — the six files `@hanzo/frame` resolves
`middleware/film.py` + the **🎞 Films** Home tab. Orthogonal to the GPU lanes and to
Office documents: a film is HTML rendered in headless Chrome, no model, no graph.

- **Two homes, and they do not mix.** `hanzoai/frames` is the RENDERER (our fork of
  the local HTML→mp4 renderer; published `@hanzo/frames@0.7.83`, bin `frames`).
  `@hanzo/frame` is the PLAYER — the one `<Frame src alt />` every Hanzo surface
  embeds. Studio is now the place the six files between them are MADE. Before this
  a film was `cp -r film/cloud film/x`, edit `film.mjs`, `make` — a developer's
  workflow living in one website's repo, seven copies of ~200 lines of hand-written
  HTML+GSAP.
- **A film IS its beats; the composition is a DERIVATION** — the same shape
  `documents.py` takes. `{title, src, alt, lead, beats:[{head,note}], foot}`. One
  generator writes BOTH shapes (`tall` 1080x1920, `wide` 1920x1080), because a phone
  is 0.46 wide-to-tall and a laptop 1.6 and hand-keeping two files that must say the
  same thing drifts on the first copy edit. **Duration is derived** from the beat
  count — a typed duration beside a list is a second copy of the list's length.
- **The expensive rules are STRUCTURAL, not remembered.** Each of them renders
  SUCCESSFULLY when it is wrong, which is why they are generated rather than
  documented: `data-width/height/fps/duration` are always written (without them
  every master renders 1080x1920 portrait); only `opacity` and `translateY` are ever
  animated (the renderer refuses layout/text-reflow tweens — they snap to integer
  device pixels and stutter under seek-by-frame capture — and we render `--strict`
  so that refusal is fatal); the lead carries NO entrance, so frame 0 (the poster) is
  never black; the last beat holds 3s, so the final frame — what every reduced-motion
  viewer is served instead of the film — is the finished thing; nothing is dimmer
  than `#777` (the measured AA floor on true black); the tall column is 840px, inside
  the middle 862 a 390x844 phone leaves after `object-fit: cover`.
- **Motion is CSS, and `data-no-timeline` is why that is free.** Capture polls for a
  GSAP timeline at `window.__timelines[id]` and, finding none, waits the full 45s
  player-ready timeout — measured 51.5s vs 5.8s for the same 4s composition. That
  attribute is the renderer's own opt-out for CSS-driven compositions
  (`packages/engine/src/services/frameCapture.ts`), and taking it means a composition
  is one self-contained file with no vendored third-party script and no licence
  question. Geist comes from the sibling `design` checkout via `design.faces()` —
  absent, the composition falls through its own font stack and still renders.
- **The render is a subprocess, and a refusal names what is missing.**
  `npx --yes @hanzo/frames@0.7.83 render --quality high --no-browser-gpu --workers 1
  --strict` (host GPU auto-probing fails capture calibration on our hardware and the
  render dies at frame 0), cwd = the project because the CLI resolves `--output`
  against the PROCESS directory; then ffmpeg cuts `-first` (frame 0) and `-last`
  (`-sseof`, the only way to name the last frame without the frame count).
  `film.tools()` reports npx/node/ffmpeg/ffprobe; missing any → **503** naming them,
  and the Films tab does not draw a Render button it knows would fail. **Studio's own
  image carries none of them**, so studio.hanzo.ai authors and stores films today and
  renders them where the renderer lives.
- **Sizes are read BACK, not assumed** (`measure()` → ffprobe): a master of the wrong
  shape renders successfully, so the response states the measured width/height/seconds.
- **The six files land through the ONE library ingest** (`_land_film` →
  `_index_upload`, injected like every other resolver), so they are `kind:"film"`
  assets with a work-log row and the truncation guard, and **☁ Publish to CMS**
  already works on them.
- REST (`/v1` house style): `GET|POST /v1/films`, `GET|PATCH|DELETE /v1/films/{id}`,
  `GET /v1/films/{id}/composition?shape=` (the derivation, inspectable),
  `POST /v1/films/{id}/render`. `POST` returns the film plus `seconds`, the six
  `names`, and `frame` — the `<Frame …/>` line to paste, verbatim.
- **Fixed while in here:** `*{margin:0}` outranks the browser's own
  `dialog:modal{margin:auto}`, so EVERY dialog Studio has ever opened sat in the
  top-left corner (measured x=0 for `docdlg`/`filmdlg`/`ctxdlg` at 1440x900; x=440
  after). The document form's inputs also had no rule at all — browser default chrome
  in a dark panel — so the dialog field style now covers inputs as well as textareas
  and `.docl` is no longer scoped to one dialog.

## Create composer + gateway auth (v0.18.1 — the intuitive first Create)
The "What should we create?" composer must PRODUCE something on the first ↑, and Chat must
reach the gateway. Both were fixed here.

- **Gateway auth is ONE fail-closed seam** (`middleware/gateway_auth.py`): the canonical
  allowlist for handing a browser-edge bearer to api.hanzo.ai. `ai_bearer(request,
  server_env=…)` = a server key first (never in page source), else the caller's own token
  ONLY when the gateway can verify it — a publishable `pk-` key or an asymmetric/JWKS JWT
  (RS/ES/PS/EdDSA). A symmetric **HS256 hanzo.id session token is NEVER forwarded** (the
  gateway rejects it → "unsupported signing method: HS256" → the old 502), nor any secret
  `sk-`/`hk-`/`rk-` key. `copilot.py._resolve_target` now uses it
  (`_SERVER_TOKEN_ENV=(STUDIO_COPILOT_TOKEN, STUDIO_COMMERCE_TOKEN)`), so the editor-ops
  chat no longer 502s on the session token. The shipped **Chat sidebar** (`shell.js`) still
  streams enso directly from the gateway with `window.HZ.pk` when present; its copilot
  fallback now renders ONE friendly line on failure — never the raw backend error/JSON.
  (documents.py's AI-fill has an equivalent private copy; it should adopt `gateway_auth`
  once its concurrent review lands — the shared module is the going-forward one way.)
- **Text-to-image is the default create lane** (`studio_home.py`: `_image_graph` +
  `dispatch_image` + `POST /v1/library/image`): the proven **Z-Image-Turbo** graph
  (`z_image_turbo_bf16` / `qwen_3_4b` lumina2 / `ae` vae; EmptySD3LatentImage →
  ModelSamplingAuraFlow shift 3 → KSampler res_multistep/simple 4 steps cfg 1.0 →
  SaveImage), lifted from the shipped blueprint, dispatched through the ONE funnel like
  video/fix. Words in → an image out; a model-less pod surfaces the real DispatchError, not
  a silent nothing.
- **Composer routes ↑ by the selected model's modality** (`studio_home.html`): default
  `MODEL='zen-image'` from `hz_create_model` — DECOUPLED from the chat model (`hz_model`),
  so an image pick never breaks the assistant (and `shell.js chatModel()` ignores a
  non-chat `hz_model`). `composerPlan()`: assistant model → hand to the ONE chat via
  `window.HanzoChat.send` (exposed by shell.js); no photo → text-to-image; 1 photo → fix;
  2+ → compose; video model/lane → video. `create()` ALWAYS pulses a "Preparing…"
  live-bar state and, on failure, shows a friendly retry (prompt kept) — never a silent
  no-op. The code model (`zen5-coder`) is hidden from the creative composer; each model has
  a one-line description; the `</>` request-preview is gated behind Developers mode
  (`body.hzdev`, set by the shell's Developers pill).
- **P2 friction fixes** (`studio_home.html` + `shell.css`): a first-class **Download** on
  every tile AND in the viewer (PNG original · JPG/WebP client-side canvas convert, the
  popover flips up near the viewport bottom); the fixed header + sticky live-bar are now
  OPAQUE (were translucent → content scrolled under them); the **ARCHIVED** badge shows
  ONLY for truly deleted/flagged items (active work is unbadged); on mobile the ~30 filter
  pills collapse to one horizontally-scrollable row and the tab bar gets a scroll fade
  affordance. The Chat empty state (shell.js) shows an assistant identity + one line + 3
  tappable starters.

## Tests
`tests-unit/{folder_paths_test,middleware_test,app_test,prompt_server_test,studio_home_test,documents_test}` —
run `uv run pytest tests-unit/folder_paths_test tests-unit/middleware_test
tests-unit/app_test tests-unit/prompt_server_test tests-unit/studio_home_test
tests-unit/documents_test -q`. The auth/tenancy work is
covered by `middleware_test/iam_auth_test.py`,
`middleware_test/session_test.py` and
`folder_paths_test/org_scoping_test.py`. Office documents:
`documents_test/test_documents.py` (catalog invariants, the full template×format render
matrix asserted by magic numbers, AI-fill parse + fail-closed bearer allowlist, the REST
surface incl. per-org isolation on a real aiohttp app, and the IR as the document: the
grammar↔renderer drift gate, authoring with no template, an IR edit reaching the file, a
fields PATCH rebuilding from the template, a retired template not orphaning a document, a
pre-IR document still rendering, and an unrenderable IR storing nothing).
Films: `middleware_test/film_test.py` (the derivation — duration from the beats, both
shapes from one generator, the four `data-*` reaching the renderer, one keyframe block
holding only opacity+transform, frame 0 not black, the contrast floor, the crop column,
`data-no-timeline`; the REST surface on a real app incl. per-org isolation, an edit still
held to the rules, the six files reaching the one library ingest, and a toolchain-less
render refused by name) and `middleware_test/film_render_test.py` — the ONE test that
leaves the process, skipped where npx/node/ffmpeg/ffprobe are absent, which measures the
rendered masters with ffprobe (1920x1080 and 1080x1920, duration = the derived seconds),
proves the poster is not a black rectangle and the last frame is richer than the first,
and renders twice to the same sha256. Create composer + gateway auth:
`middleware_test/gateway_auth_test.py` (the fail-closed allowlist — HS256/`sk-` refused,
`pk-`/asymmetric forwarded, server-key precedence, session-cookie path),
`middleware_test/copilot_test.py::test_resolve_target_never_forwards_hs256_session_token`,
and `studio_home_test/create_image_test.py` (the Z-Image-Turbo graph shape, `dispatch_image`
validation, and served-HTML invariants: default-model-is-image, text→image route,
Create-always-shows-a-state, first-class Download, no ARCHIVED badge on active items, opaque
bars, gated `</>`, collapsed mobile filters). Library provenance + CMS publish:
`studio_home_test/publish_test.py`.
