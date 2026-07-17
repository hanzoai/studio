// The studio-chat data layer. Two backends, no proxy layer between them:
//   - the agent orchestrator: POST api.hanzo.ai/v1/agent  (github.com/hanzoai/agent,
//     mounted in the cloud binary) — called DIRECTLY, no studio pass-through. The
//     hanzo_token cookie is scoped to .hanzo.ai so credentials:'include' carries it
//     cross-origin, and the round replays it into the per-org-billed completion.
//     (Gateway CORS must allow the studio.hanzo.ai origin with credentials.)
//   - the studio engine API: /v1/session, /v1/library, /v1/render-queue, … —
//     same-origin on studio.hanzo.ai (same cookie).
// No token handling in the browser: the cookie is the credential for both.
// VITE_STUDIO_API / VITE_AGENT_API override the bases for local dev.

const BASE = (import.meta.env.VITE_STUDIO_API ?? '').replace(/\/$/, '')
// Default: same-origin through the studio's /v1/agent bridge — the host-only
// session cookie authorizes it and the middleware forwards the bearer, so no
// parent-domain cookie and no gateway CORS dependency. VITE_AGENT_API points
// straight at the gateway for direct mode.
const AGENT_API = (import.meta.env.VITE_AGENT_API ?? '').replace(/\/$/, '')

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { credentials: 'include', ...init })
  if (!r.ok) throw new Error(`${url} -> ${r.status}`)
  return r.json() as Promise<T>
}

export type Role = 'user' | 'assistant'
export interface ChatMessage { role: Role; content: string }
export interface Action { name: string; args?: Record<string, unknown>; result?: unknown; error?: string }
export interface Op { name: string; args?: Record<string, unknown> }
export interface ChatReply { reply: string; actions: Action[]; ops: Op[]; conversationId: string }

// One turn of a preset: the model edits/combines library assets; `actions` are the
// renders it dispatched (each result carries a prompt_id). Pass the prior reply's
// conversationId to continue a thread — the orchestrator persists per-org history.
export function chat(messages: ChatMessage[], conversationId?: string, preset = 'create'): Promise<ChatReply> {
  return j<ChatReply>(`${AGENT_API}/v1/agent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ preset, messages, conversationId }),
  })
}

export interface Session {
  user?: string; email?: string; org?: string; orgs?: string[]
  authenticated?: boolean; iam_url?: string
}
export const getSession = () => j<Session>(`${BASE}/v1/session`).catch(() => ({ authenticated: false } as Session))

export interface Asset { path: string; status?: string; mtime?: number; view?: unknown }
export const getLibrary = () => j<{ org: string; assets: Asset[] }>(`${BASE}/v1/library`).catch(() => ({ org: '', assets: [] }))

export interface RenderJob { id: string; prefix?: string; refs?: string[]; ts?: number; age?: number; status?: string }
export const getRenderQueue = () => j<{ jobs: RenderJob[] }>(`${BASE}/v1/render-queue`).catch(() => ({ jobs: [] }))
export const gpuOnline = () => j<{ online: boolean }>(`${BASE}/v1/gpu-status`).then(r => r.online).catch(() => false)

// Thumbnail URL for a library asset (the studio pod serves a downsized JPEG).
export const thumbURL = (path: string) => `${BASE}/v1/library/file?thumb=1&path=${encodeURIComponent(path)}`
export const fileURL = (path: string) => `${BASE}/v1/library/file?path=${encodeURIComponent(path)}`
