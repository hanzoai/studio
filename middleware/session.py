"""Identity / session API for the Hanzo Studio user menu.

Thin ``/v1`` endpoints over the IAM-validated context the auth middleware
already produces (``request["iam_user"]``). One module owns the session shape
the frontend menu reads and the active-org / active-project selection; the auth
middleware owns token validation and imports :func:`resolve_org` so the switched
org drives the *same* ``iam_user["org_id"]`` the tenancy / output paths use.

    GET  /v1/session          -> {user,email,org,orgs[],project,projects[],...}
    POST /v1/session/org      -> set active org (must be one the user belongs to)
    POST /v1/session/project  -> set active project (a workflow-workspace folder)
    GET  /logout              -> clear the session + active-org/project cookies,
                                 bounce to the IAM end-session then back to /login

Works with auth on (real IAM user) or off (local single-user): with auth off the
request carries no ``iam_user`` so the local user is synthesized from env +
cookies, and the menu still renders and switches for local development.
"""
import os
from urllib.parse import urlencode

from aiohttp import web

import folder_paths

# Cookies. The session (auth) cookie name mirrors the auth middleware.
SESSION_COOKIE = "hanzo_token"
STATE_COOKIE = "studio_oauth"
ACTIVE_ORG_COOKIE = "studio_active_org"
ACTIVE_PROJECT_COOKIE = "studio_active_project"

DEFAULT_PROJECT = "default"


def _cfg(attr: str, env: str, default: str) -> str:
    """One config read: the CLI arg (which itself defaults to the env var) wins,
    with a plain env / literal fallback so unit tests need no arg parsing."""
    try:
        from studio.cli_args import args
        val = getattr(args, attr, None)
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(env, default)


def resolve_org(base_org: str, orgs: list[str], cookie_val: str | None) -> str:
    """Return the effective org: the cookie-selected one iff the user belongs to
    it, else the token's own org. This is the *single* rule the auth middleware
    and the session API both apply, so a switch can never escape membership."""
    if cookie_val and cookie_val in orgs:
        return cookie_val
    return base_org


def _external_base(request: web.Request) -> str:
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"


def _local_user() -> dict:
    org = os.environ.get("STUDIO_ORG_ID") or "default"
    return {
        "sub": "local", "name": "Local User", "email": "",
        "org_id": org, "avatar": "", "orgs": [org],
    }


def _projects(org_id: str) -> list[str]:
    """Projects = the workflow-workspace subfolders under the org's user dir
    (user/[orgs/<org>/]user/<uid>/workflows/<project>). Best-effort; the default
    project is always present."""
    names = {DEFAULT_PROJECT}
    try:
        base = folder_paths.get_org_user_directory(org_id)
        for uid in os.listdir(base):
            wf = os.path.join(base, uid, "workflows")
            if not os.path.isdir(wf):
                continue
            for p in os.listdir(wf):
                if os.path.isdir(os.path.join(wf, p)):
                    names.add(p)
    except (OSError, FileNotFoundError):
        pass
    return sorted(names)


def _session(request: web.Request) -> dict:
    user = request.get("iam_user") or _local_user()
    orgs = user.get("orgs") or [user.get("org_id", "default")]
    # When auth is on the middleware already applied the override; applying the
    # same validated rule here is idempotent and covers the auth-off local path.
    org = resolve_org(user.get("org_id", "default"), orgs, request.cookies.get(ACTIVE_ORG_COOKIE))
    project = request.cookies.get(ACTIVE_PROJECT_COOKIE) or DEFAULT_PROJECT
    return {
        "authenticated": bool(request.get("iam_user")),
        "sub": user.get("sub", ""),
        "user": user.get("name") or user.get("email") or "User",
        "email": user.get("email", ""),
        "avatar": user.get("avatar", ""),
        "org": org,
        "orgs": orgs,
        "project": project,
        "projects": _projects(org),
        "iam_url": _cfg("iam_url", "STUDIO_IAM_URL", "https://hanzo.id"),
        "console_url": _cfg("console_url", "STUDIO_CONSOLE_URL", "https://console.hanzo.ai"),
    }


def _is_https(request: web.Request) -> bool:
    return _external_base(request).startswith("https")


def add_session_routes(routes: web.RouteTableDef, server) -> None:
    """Register the session / identity endpoints on the app route table."""

    iam_url = _cfg("iam_url", "STUDIO_IAM_URL", "https://hanzo.id").rstrip("/")

    @routes.get("/v1/session")
    async def get_session(request):
        return web.json_response(_session(request))

    @routes.post("/v1/session/org")
    async def set_org(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        org = (body or {}).get("org")
        if not org or not isinstance(org, str):
            return web.json_response({"error": "org is required"}, status=400)
        user = request.get("iam_user") or _local_user()
        orgs = user.get("orgs") or [user.get("org_id", "default")]
        if org not in orgs:
            return web.json_response({"error": "not a member of that org", "orgs": orgs}, status=403)
        resp = web.json_response({"ok": True, "org": org})
        resp.set_cookie(
            ACTIVE_ORG_COOKIE, org, max_age=31536000,
            httponly=False, secure=_is_https(request), samesite="Lax", path="/",
        )
        # Switching org resets the project scope to the org's default.
        resp.del_cookie(ACTIVE_PROJECT_COOKIE, path="/")
        return resp

    @routes.post("/v1/session/project")
    async def set_project(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        project = (body or {}).get("project")
        if not project or not isinstance(project, str) or "/" in project or "\\" in project:
            return web.json_response({"error": "valid project is required"}, status=400)
        resp = web.json_response({"ok": True, "project": project})
        resp.set_cookie(
            ACTIVE_PROJECT_COOKIE, project, max_age=31536000,
            httponly=False, secure=_is_https(request), samesite="Lax", path="/",
        )
        return resp

    async def logout(request):
        """Clear the local session + selection cookies and bounce to the IAM
        end-session endpoint (so SSO is really dropped, not silently re-issued),
        which returns the browser to ``/login`` for a fresh sign-in."""
        base = _external_base(request)
        logout_url = os.environ.get("STUDIO_IAM_LOGOUT_URL", f"{iam_url}/v1/iam/oauth/logout")
        target = f"{logout_url}?{urlencode({'post_logout_redirect_uri': base + '/login', 'redirect_uri': base + '/login'})}"
        resp = web.HTTPFound(target)
        for name in (SESSION_COOKIE, "access_token", ACTIVE_ORG_COOKIE, ACTIVE_PROJECT_COOKIE, STATE_COOKIE):
            resp.del_cookie(name, path="/")
        return resp

    routes.get("/logout")(logout)
    routes.post("/logout")(logout)
