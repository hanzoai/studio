// The studio-chat data layer. Two backends, both same-origin (studio.hanzo.ai):
//   - the cloud chat orchestrator: POST /v1/chat  (proxied same-origin; see note)
//   - the studio engine API:       /v1/session, /v1/library, /v1/render-queue, …
// Every call rides the same-origin hanzo_token cookie (credentials:'include'),
// so there is no token handling or CORS in the browser.
//
// NOTE on /v1/chat: the canonical orchestrator lives in the cloud binary
// (clients/chat). Studio proxies /v1/chat to it so the browser stays same-origin
// and per-org billing runs cloud-side. VITE_STUDIO_API overrides the base for
// local dev against a remote studio.

const BASE = (import.meta.env.VITE_STUDIO_API ?? '').replace(/\/$/, '')

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, { credentials: 'include', ...init })
  if (!r.ok) throw new Error(`${path} -> ${r.status}`)
  return r.json() as Promise<T>
}

export type Role = 'user' | 'assistant'
export interface ChatMessage { role: Role; content: string }
export interface Action { name: string; args?: Record<string, unknown>; result?: unknown; error?: string }
export interface Op { name: string; args?: Record<string, unknown> }
export interface ChatReply { reply: string; actions: Action[]; ops: Op[] }

// One turn of the create capability: the model edits/combines library assets;
// `actions` are the renders it dispatched (each result carries a prompt_id).
export function chat(messages: ChatMessage[], capability = 'create'): Promise<ChatReply> {
  return j<ChatReply>('/v1/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ capability, messages }),
  })
}

export interface Session {
  user?: string; email?: string; org?: string; orgs?: string[]
  authenticated?: boolean; iam_url?: string
}
export const getSession = () => j<Session>('/v1/session').catch(() => ({ authenticated: false } as Session))

export interface Asset { path: string; status?: string; mtime?: number; view?: unknown }
export const getLibrary = () => j<{ org: string; assets: Asset[] }>('/v1/library').catch(() => ({ org: '', assets: [] }))

export interface RenderJob { id: string; prefix?: string; refs?: string[]; ts?: number; age?: number; status?: string }
export const getRenderQueue = () => j<{ jobs: RenderJob[] }>('/v1/render-queue').catch(() => ({ jobs: [] }))
export const gpuOnline = () => j<{ online: boolean }>('/v1/gpu-status').then(r => r.online).catch(() => false)

// Thumbnail URL for a library asset (the studio pod serves a downsized JPEG).
export const thumbURL = (path: string) => `${BASE}/v1/library/file?thumb=1&path=${encodeURIComponent(path)}`
export const fileURL = (path: string) => `${BASE}/v1/library/file?path=${encodeURIComponent(path)}`
