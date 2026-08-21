// The studio-chat data layer. Every call is same-origin on studio.hanzo.ai and
// carries one credential: the host-only httpOnly session cookie.
//   - the conversation round: POST /v1/agents/chat — studio's own route reads the
//     caller's IAM token from that cookie and forwards it to the cloud agents
//     surface server-side, so the token is never readable from the page.
//   - the studio engine API: /v1/session, /v1/worklog, /v1/queue/*, /v1/favorites,
//     /v1/stacks, …
// `same-origin` rather than `include`: a credential cannot leave this origin even
// if a base is ever pointed elsewhere.
// VITE_STUDIO_API overrides the base for local dev.

const BASE = (import.meta.env.VITE_STUDIO_API ?? '').replace(/\/$/, '')

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { credentials: 'same-origin', ...init })
  if (!r.ok) {
    // Surface the server's own message — a 402 says "add credits at
    // pay.hanzo.ai", which beats a bare status code.
    let msg = `${url} -> ${r.status}`
    try {
      const b = await r.json()
      const m = b?.error?.message ?? b?.error ?? b?.message
      if (typeof m === 'string' && m) msg = m
    } catch { /* keep the status line */ }
    throw new Error(msg)
  }
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
  return j<ChatReply>(`${BASE}/v1/agents/chat`, {
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
export const gpuOnline = () => j<{ online: boolean }>(`${BASE}/v1/gpu-status`).then(r => r.online).catch(() => false)

// A run in a flow: one dispatched generation (fix/compose/rerun/render/template) with
// its instruction, references, parents (the assets it derived from), landed output and
// real status. The queue/history backbone — GET /v1/worklog, newest first.
export type RunStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
export interface Run {
  id: string; ts: number; kind: string; prompt: string
  refs: string[]; uploads: string[]; parents: string[]
  output_prefix: string; output: string | null
  lane: string; node: string; status: RunStatus
  started_at?: number; finished_at?: number; error?: string
  params?: Record<string, unknown>
}
export const getWorklog = (q?: string) =>
  j<{ org: string; items: Run[] }>(`${BASE}/v1/worklog${q ? `?q=${encodeURIComponent(q)}` : ''}`)
    .catch(() => ({ org: '', items: [] as Run[] }))

// Live position + ETA overlay for the ACTIVE runs, per lane, from the org's own
// measured medians. Joined onto the worklog rows by id.
export interface QueueItem {
  id: string; kind: string; prompt: string; status: string; node: string
  lane?: string; position?: number; of?: number
  eta_seconds?: number; wait_seconds?: number; elapsed_seconds?: number; remaining_seconds?: number | null
}
export interface QueueStatus { items: Record<string, QueueItem>; medians: Record<string, number>; lanes: Record<string, number>; now: number }
export const getQueueStatus = () =>
  j<QueueStatus>(`${BASE}/v1/queue/status`).catch(() => ({ items: {}, medians: {}, lanes: {}, now: Math.floor(Date.now() / 1000) }))

// The version chain of one output — ancestors (what it derived from) + descendants
// (later iterations). The iteration history of a design flow.
export interface LineageStep { path: string; prompt: string; kind: string; ts: number }
export interface Lineage { ancestors: LineageStep[]; node: string; descendants: LineageStep[] }
export const getLineage = (path: string) =>
  j<Lineage>(`${BASE}/v1/worklog/lineage?path=${encodeURIComponent(path)}`)
    .catch(() => ({ ancestors: [], node: path, descendants: [] }))

// Delete a queued run (tenant-scoped cancel; a foreign id 404s).
export const cancelRun = (id: string) =>
  j<{ ok: boolean; canceled: string }>(`${BASE}/v1/queue/cancel`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt_id: id }),
  })

// Amend a still-queued run — the ONE way to change it (atomic cancel + re-dispatch
// with the same instruction plus added references). Adding references IS an amend.
export const amendRun = (id: string, instruction: string, uploads: string[], paths: string[] = []) =>
  j<{ ok: boolean; prompt_id: string; amended_from: string; refs: number }>(`${BASE}/v1/queue/amend`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt_id: id, instruction, uploads, paths }),
  })

// Stage a reference image in the org input dir (the engine's upload route); returns
// the flat name amend/compose reference by.
export const uploadRef = (file: File) => {
  const fd = new FormData()
  fd.append('image', file, file.name)
  return j<{ name: string; subfolder: string; type: string }>(`${BASE}/upload/image`, { method: 'POST', body: fd })
}

// Ratings feed the AI flywheel: 0-5 stars + a note, keyed by run id, carrying the
// output path. Persisted per-org; visible on reopen.
export interface Rating { stars?: number; notes?: string; path?: string; updatedAt?: number }
export const getRatings = () => j<Record<string, Rating>>(`${BASE}/v1/library/ratings`).catch(() => ({}))
export const rate = (key: string, patch: { stars?: number; notes?: string; path?: string }) =>
  j<{ ok: boolean; key: string; entry: Rating }>(`${BASE}/v1/library/rate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key, ...patch }),
  })

// Soft-delete a finished result — the library status lifecycle ('deleted'), the same
// mechanism the assets view uses. Keyed by the run's output path.
export const softDelete = (path: string) =>
  j<{ ok: boolean; path: string; status: string }>(`${BASE}/v1/library/status`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, status: 'deleted' }),
  })
// The org's soft-deleted asset paths — so History hides results deleted here or in the
// assets view, and the delete persists across reloads.
export const getDeletedPaths = () =>
  j<{ assets: { path: string; status?: string }[] }>(`${BASE}/v1/library`)
    .then(r => (r.assets || []).filter(a => a.status === 'deleted').map(a => a.path))
    .catch(() => [] as string[])

// Favorites: the heart toggle, a per-org set keyed by run id. Separate concern from
// ratings — a bookmark, not quality feedback.
export const getFavorites = () =>
  j<{ org: string; favorites: Record<string, { ts: number }> }>(`${BASE}/v1/favorites`)
    .then(r => r.favorites).catch(() => ({} as Record<string, { ts: number }>))
export const setFavorite = (key: string, favorite: boolean) =>
  j<{ ok: boolean; key: string; favorite: boolean }>(`${BASE}/v1/favorites`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key, favorite }),
  })

// Stacks: iOS-folder groupings of runs. The client owns the arrangement; the whole
// normalized set is POSTed back and echoed.
export interface Stack { id: string; name: string; items: string[]; ts: number }
export const getStacks = () =>
  j<{ org: string; stacks: Stack[] }>(`${BASE}/v1/stacks`).then(r => r.stacks).catch(() => [] as Stack[])
export const saveStacks = (stacks: Stack[]) =>
  j<{ org: string; stacks: Stack[] }>(`${BASE}/v1/stacks`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ stacks }),
  }).then(r => r.stacks)

// Favorite → template: capture a reusable FLOW (kind + instruction + refs + parents).
export interface Flow { kind: string; prompt: string; refs: string[]; parents: string[] }
export const saveTemplate = (name: string, flow: Flow) =>
  j<{ ok: boolean; name: string; slug: string }>(`${BASE}/v1/templates`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, flow }),
  })

// Thumbnails: a landed library output, a staged input reference, and the full-res file.
export const thumbURL = (path: string) => `${BASE}/v1/library/file?thumb=1&path=${encodeURIComponent(path)}`
export const inputThumbURL = (name: string) => `${BASE}/v1/library/file?input=1&thumb=1&path=${encodeURIComponent(name)}`
export const fileURL = (path: string) => `${BASE}/v1/library/file?path=${encodeURIComponent(path)}`
