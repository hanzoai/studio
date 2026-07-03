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
  `/v1/worker/execute` (push). Worker↔coordinator trust via
  `X-Worker-Token` (`STUDIO_WORKER_TOKEN`, KMS-sourced). NAT'd local boxes (GB10)
  join outbound — pull channel is specced in `docs/federation.md`.

## Tests
`tests-unit/{folder_paths_test,middleware_test,app_test,prompt_server_test}` —
run `uv run pytest tests-unit/folder_paths_test tests-unit/middleware_test
tests-unit/app_test tests-unit/prompt_server_test -q`. The auth/tenancy work is
covered by `middleware_test/iam_auth_test.py` and
`folder_paths_test/org_scoping_test.py`.
