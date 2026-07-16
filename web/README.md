# Studio web — the Hanzo Studio product frontend

Our unique studio frontend: a specialized **chat app** on the shared Hanzo stack.
You say what to create; the cloud `/v1/chat` orchestrator drives studio's render
tools and the outputs land in the library. This is the default surface at
`studio.hanzo.ai/studio`.

- **Stack**: Vite + React + TS. `@hanzo/*` packages are npm-published, so a clean
  `npm install` — no workspace linking.
- **Chat**: `POST api.hanzo.ai/v1/chat {capability, messages}` → `{reply, actions, ops}`
  — the cloud `clients/chat` orchestrator, called **directly** (no studio proxy). The
  `hanzo_token` cookie is `.hanzo.ai`-scoped, so `credentials:'include'` carries it
  cross-origin and the handler bills the caller org. (Gateway CORS must allow the
  `studio.hanzo.ai` origin with credentials.)
- **Engine**: the studio pod serves `/v1/session`, `/v1/library`, `/v1/render-queue`,
  `/v1/gpu-status` same-origin (same cookie) — no token handling in the browser.

```bash
npm install
npm run build      # -> dist/  (served at /studio; base = /studio/)
npm run dev        # local, set VITE_STUDIO_API=https://studio.hanzo.ai
```

The ComfyUI-derived node editor ("Advanced mode") is a **separate** repo —
`hanzoai/studio-ui` (formerly `studio-frontend`) — not this app.

## Status / next
- Increment 1 (done): chat wired to `/v1/chat` create + live library/queue/gpu strip.
- Increment 2: swap the transcript/composer for `@hanzo/chat` `<Chat>` on `@hanzo/gui`
  (GuiProvider + SessionProvider, copying console2's `Provider.tsx` spine).
- Wiring: Dockerfile multi-stage to build `dist/` and serve it at `/studio` in
  place of `middleware/studio_home.html`. (No proxy — chat hits api.hanzo.ai direct.)
