/**
 * THE ONE CLIENT MODULE. Everything Studio asks of the network goes through
 * here, so the transport is one change when the native workflow plane
 * (`api.hanzo.ai/v1/studio/*`) answers.
 *
 * Three places are reached today:
 *
 *   gateway   api.hanzo.ai/v1: the models — the chat that decides what to make,
 *             image generation and edit, speech, music. Billed to `org`.
 *   store     this browser's IndexedDB, per person and org, and the person's
 *             private Library (api.hanzo.ai/v1/library) once it answers.
 *   engine    the studio engine: documents and decks, and the GPU lanes
 *             (Zen Image, Wan video, ACE music, mesh) dispatched to the org's
 *             GPUs. Same origin on studio.hanzo.ai; https://studio.hanzo.ai
 *             from anywhere else, with the bearer, never a cookie.
 *
 * A refusal is a `Refusal` carrying the status and the server's own sentence,
 * whichever envelope it came in (problem details, `{error}`, `{status:'error',
 * msg}`), and a 200 that says `status: "error"` is a refusal too.
 */

export interface Wire {
  /** The platform origin, no trailing slash. */
  api: string
  /** The engine origin, no trailing slash: '' for this origin. */
  engine: string
  /** The signed-in person's bearer, read at call time. */
  token: () => string | null
  /** The org every call is scoped to (`X-Org-Id`). */
  org: string | null
}

/** Where the engine is from a page that is not studio.hanzo.ai. */
export const ENGINE = 'https://studio.hanzo.ai'

export class Refusal extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code = '',
  ) {
    super(message)
    this.name = 'Refusal'
  }
}

type Base = 'api' | 'engine'

const origin = (w: Wire, base: Base) => (base === 'api' ? w.api : w.engine)

function headers(w: Wire, extra?: Record<string, string>): Headers {
  const h = new Headers(extra)
  const t = w.token()
  if (t) h.set('Authorization', `Bearer ${t}`)
  if (w.org) h.set('X-Org-Id', w.org)
  return h
}

/** The sentence a refusal carries, and its code. */
async function reason(res: Response): Promise<[string, string]> {
  try {
    const b = (await res.json()) as Record<string, unknown>
    const err = b.error as { message?: string; code?: string } | string | undefined
    const said = typeof err === 'string' ? err : err?.message
    const msg = [b.detail, said, b.msg, b.message, b.title].find((v) => typeof v === 'string' && v) as string | undefined
    return [msg ?? '', String(b.code ?? (typeof err === 'object' ? err?.code : '') ?? '')]
  } catch {
    return ['', '']
  }
}

/** A 200 in the `{status:'error', msg}` envelope is a refusal. */
function envelope(v: unknown): unknown {
  const r = v as { status?: unknown; msg?: unknown; code?: unknown } | null
  if (r && typeof r === 'object' && r.status === 'error') {
    throw new Refusal(400, typeof r.msg === 'string' && r.msg ? r.msg : 'The platform refused that.', String(r.code ?? ''))
  }
  return v
}

export interface Ask {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  /** A body sent as is (a form, a blob). */
  raw?: BodyInit
  type?: string
  signal?: AbortSignal
  head?: Record<string, string>
  /** Reads the headers of an answer that succeeded. */
  heard?: (h: Headers) => void
}

/** One request. JSON in, JSON out, unless the answer is not JSON. */
export async function ask<T>(w: Wire, base: Base, path: string, o: Ask = {}): Promise<T> {
  const h = headers(w, o.head)
  let body: BodyInit | undefined = o.raw
  if (o.body !== undefined) {
    h.set('Content-Type', 'application/json')
    body = JSON.stringify(o.body)
  } else if (o.type) h.set('Content-Type', o.type)
  const res = await fetch(`${origin(w, base)}${path}`, {
    method: o.method ?? (body === undefined ? 'GET' : 'POST'),
    headers: h,
    body,
    cache: 'no-store',
    credentials: base === 'engine' && !w.engine ? 'same-origin' : 'omit',
    signal: o.signal,
  })
  if (!res.ok) {
    const [msg, code] = await reason(res)
    throw new Refusal(res.status, msg || `${path.split('?')[0]} answered ${res.status}`, code)
  }
  o.heard?.(res.headers)
  if (res.status === 204) return undefined as T
  const type = res.headers.get('content-type') ?? ''
  if (type.includes('json')) return envelope(await res.json()) as T
  if (type.startsWith('text/')) return (await res.text()) as T
  return (await res.blob()) as T
}

/** `?a=1&b=2` from the defined, non-empty values. */
export function query(q: Record<string, string | number | boolean | null | undefined>): string {
  const s = new URLSearchParams()
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== '') s.set(k, String(v))
  const out = s.toString()
  return out ? `?${out}` : ''
}

const enc = encodeURIComponent

/* ----------------------------------------------------------------- gateway */

export interface Call {
  id: string
  name: string
  /** The arguments as the model wrote them: JSON text. */
  args: string
}

export interface Said {
  text: string
  calls: Call[]
  /** The model that answered, as the gateway names it. */
  model: string
}

export type Message =
  | { role: 'system' | 'user'; content: string | Part[] }
  | { role: 'assistant'; content: string; tool_calls?: { id: string; type: 'function'; function: { name: string; arguments: string } }[] }
  | { role: 'tool'; tool_call_id: string; content: string }

export type Part = { type: 'text'; text: string } | { type: 'image_url'; image_url: { url: string } }

export interface Tool {
  type: 'function'
  function: { name: string; description: string; parameters: object }
}

/**
 * One turn of the chat that decides what to make, streamed: `heard` gets the
 * words so far as they arrive. Answers the words and the tool calls in full.
 */
export async function complete(
  w: Wire,
  body: { model: string; messages: Message[]; tools?: Tool[]; temperature?: number },
  heard: (text: string) => void,
  signal?: AbortSignal,
): Promise<Said> {
  const res = await fetch(`${w.api}/v1/chat/completions`, {
    method: 'POST',
    headers: headers(w, { 'Content-Type': 'application/json', Accept: 'text/event-stream' }),
    body: JSON.stringify({ ...body, stream: true }),
    cache: 'no-store',
    credentials: 'omit',
    signal,
  })
  if (!res.ok || !res.body) {
    const [msg, code] = await reason(res)
    throw new Refusal(res.status, msg || `The model answered ${res.status}`, code)
  }
  const type = res.headers.get('content-type') ?? ''
  // A gateway that answers whole (an error envelope, or no stream) is read whole.
  if (!type.includes('event-stream')) {
    const whole = envelope(await res.json()) as { model?: string; choices?: { message?: { content?: string; tool_calls?: { id: string; function: { name: string; arguments: string } }[] } }[] }
    const m = whole.choices?.[0]?.message
    const text = m?.content ?? ''
    heard(text)
    return { text, model: whole.model ?? body.model, calls: (m?.tool_calls ?? []).map((c) => ({ id: c.id, name: c.function.name, args: c.function.arguments })) }
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let text = ''
  let model = body.model
  const calls = new Map<number, Call>()
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let cut: number
    while ((cut = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, cut).trim()
      buffer = buffer.slice(cut + 1)
      if (!line.startsWith('data:')) continue
      const data = line.slice(5).trim()
      if (data === '[DONE]') continue
      let chunk: {
        model?: string
        error?: { message?: string; code?: string }
        choices?: { delta?: { content?: string | null; tool_calls?: { index: number; id?: string; function?: { name?: string; arguments?: string } }[] } }[]
      }
      try {
        chunk = JSON.parse(data)
      } catch {
        continue
      }
      if (chunk.error) throw new Refusal(502, chunk.error.message ?? 'The model stopped with an error.', chunk.error.code ?? '')
      if (chunk.model) model = chunk.model
      const delta = chunk.choices?.[0]?.delta
      if (delta?.content) {
        text += delta.content
        heard(text)
      }
      for (const t of delta?.tool_calls ?? []) {
        const had = calls.get(t.index) ?? { id: '', name: '', args: '' }
        calls.set(t.index, {
          id: t.id || had.id,
          name: had.name + (t.function?.name ?? ''),
          args: had.args + (t.function?.arguments ?? ''),
        })
      }
    }
  }
  return { text, model, calls: [...calls.values()].filter((c) => c.name).map((c, i) => ({ ...c, id: c.id || `call_${i}` })) }
}

/** A JSON answer from a model, asked for by schema: what a generator (a type system, a deck's words) is made of. */
export async function structured<T>(w: Wire, model: string, system: string, user: string, schema: object, signal?: AbortSignal): Promise<T> {
  const out = await ask<{ choices?: { message?: { content?: string } }[] }>(w, 'api', '/v1/chat/completions', {
    body: {
      model,
      messages: [
        { role: 'system', content: system },
        { role: 'user', content: user },
      ],
      response_format: { type: 'json_schema', json_schema: { name: 'answer', strict: true, schema } },
    },
    signal,
  })
  const text = out.choices?.[0]?.message?.content ?? ''
  const json = text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1)
  try {
    return JSON.parse(json) as T
  } catch {
    throw new Refusal(502, 'The model answered with no JSON.')
  }
}

export interface Picture {
  blob: Blob
  mime: string
}

/** A data: URL's bytes. */
export function bytes(url: string): Picture {
  const comma = url.indexOf(',')
  const head = url.slice(5, comma)
  const mime = head.split(';')[0] || 'image/png'
  const bin = atob(url.slice(comma + 1))
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return { blob: new Blob([out], { type: mime }), mime }
}

/**
 * An image from a gateway image model: words, and the pictures it starts from
 * (an edit). The image models are chat models with an image output, so this is
 * a chat completion asking for both modalities; the pictures come back on the
 * message (`images`) or among its content parts.
 */
export async function draw(w: Wire, model: string, prompt: string, o: { from?: string[]; aspect?: string; signal?: AbortSignal } = {}): Promise<Picture[]> {
  let held: Headers | undefined
  const content: Part[] = [{ type: 'text', text: prompt }, ...(o.from ?? []).map((url) => ({ type: 'image_url' as const, image_url: { url } }))]
  const out = await ask<{
    choices?: { message?: { content?: string | { type: string; image_url?: { url?: string } }[]; images?: { image_url?: { url?: string } }[] } }[]
  }>(w, 'api', '/v1/chat/completions', {
    body: {
      model,
      modalities: ['image', 'text'],
      messages: [{ role: 'user', content }],
      ...(o.aspect ? { image_config: { aspect_ratio: o.aspect } } : {}),
    },
    signal: o.signal,
    heard: (h) => (held = h),
  })
  const m = out.choices?.[0]?.message
  const urls = [
    ...(m?.images ?? []).map((i) => i.image_url?.url ?? ''),
    ...(Array.isArray(m?.content) ? m.content.map((p) => (p.type === 'image_url' ? (p.image_url?.url ?? '') : '')) : []),
  ].filter((u) => u.startsWith('data:image/'))
  if (!urls.length) throw withheld(model, held)
  return urls.map(bytes)
}

/*
 * The gateway answers a request it will not serve with a model it will, in
 * words: X-Hanzo-Usage-Reason says why and X-Hanzo-Upgrade-Url or
 * X-Hanzo-Topup-Url what lifts it. A picture request answered that way is a
 * refusal of the lane, said in the gateway's own reason.
 */
const WITHHELD: Record<string, string> = {
  paid_plan_required: 'Pictures from this model need a paid plan',
  plan_allowance_used: "This plan's pictures are used for now",
  usage_cap_exceeded: "This org's usage cap is reached",
  free_plan_cap: 'The free plan has no more pictures today',
  model_cap: "This model's share is used for now",
}

function withheld(model: string, h?: Headers): Refusal {
  const why = h?.get('X-Hanzo-Usage-Reason') ?? ''
  if (!why) return new Refusal(502, `${model} answered with no picture.`)
  const fix = h?.get('X-Hanzo-Upgrade-Url') || h?.get('X-Hanzo-Topup-Url') || ''
  return new Refusal(402, `${WITHHELD[why] ?? 'Pictures are not available on this plan'}${fix ? `: ${fix}` : '.'}`, why)
}

/** Speech from words: the platform's voice. */
export async function speak(w: Wire, input: string, voice: string, model: string, signal?: AbortSignal): Promise<Blob> {
  return ask<Blob>(w, 'api', '/v1/audio/speech', { body: { model, input, voice, response_format: 'mp3' }, signal })
}

/* ------------------------------------------------------------------- store */

/*
 * WHERE WHAT IS MADE IS KEPT. Everything a version makes (files/…) is kept in
 * the person's Library, api.hanzo.ai/v1/library: theirs alone until they share
 * it, filed with `source: studio`, `ref` the conversation, `origin`
 * /studio/c/<conversation>, and the file's key as the save's idempotency key,
 * so a save sent twice is one item. An org bucket is never written: every
 * member of the org can list one through /v1/s3.
 *
 * This browser holds, in a database per person and org, an index from each key
 * to its Library item (no bytes), and the conversations themselves (chats/…)
 * until /v1/studio keeps them. Bytes stay here only while the Library cannot
 * take them: a save it refused for now, or a file over its 10 MiB browser
 * limit. Files a browser kept before the Library answered are carried up on
 * the next visit, and then dropped here (`sync`).
 *
 *   chats/<id>.json, chats/index.json        conversations and their list
 *   files/<kind>/<chat>/<made>/<version>/<n>  every file a version made
 */

interface Record_ {
  key: string
  /** The bytes, while this browser is where they are kept. */
  blob?: Blob
  mime: string
  /** Bytes; absent on a record written before the Library answered (its blob says). */
  size?: number
  /** Unix ms. */
  modified: number
  /** Its Library item. */
  lib?: string
}

/** The person the bearer names: whose database this is. */
function subject(w: Wire): string {
  const t = w.token()
  if (!t) return ''
  try {
    const part = t.split('.')[1]!.replace(/-/g, '+').replace(/_/g, '/')
    const c = JSON.parse(atob(part + '='.repeat((4 - (part.length % 4)) % 4))) as { sub?: unknown }
    return typeof c.sub === 'string' ? c.sub : ''
  } catch {
    return ''
  }
}

const dbs = new Map<string, Promise<IDBDatabase>>()

/** This person's database in this org, opened once. */
function db(w: Wire): Promise<IDBDatabase> {
  const who = subject(w)
  if (!who || !w.org) return Promise.reject(new Refusal(401, 'Sign in to keep what you make.'))
  const name = `hanzo-studio:${w.org}:${who}`
  let open = dbs.get(name)
  if (!open) {
    open = new Promise<IDBDatabase>((resolve, reject) => {
      const req = indexedDB.open(name, 1)
      req.onupgradeneeded = () => req.result.createObjectStore('files', { keyPath: 'key' })
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => reject(new Refusal(507, 'This browser would not keep the file.'))
    })
    dbs.set(name, open)
    open.catch(() => dbs.delete(name))
  }
  return open
}

function tx<T>(d: IDBDatabase, mode: IDBTransactionMode, run: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const req = run(d.transaction('files', mode).objectStore('files'))
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(new Refusal(507, 'This browser would not keep the file.'))
  })
}

const get = async (w: Wire, key: string) => (await tx<Record_ | undefined>(await db(w), 'readonly', (s) => s.get(key))) ?? null
const keep = async (w: Wire, r: Record_) => void (await tx(await db(w), 'readwrite', (s) => s.put(r)))
const all = async (w: Wire) => tx<Record_[]>(await db(w), 'readonly', (s) => s.getAll())

/* The Library: answered or not, asked once per page. */

let library: Promise<boolean> | null = null

/** Whether api.hanzo.ai/v1/library answers for this person. */
export function libraryLive(w: Wire): Promise<boolean> {
  library ??= ask(w, 'api', '/v1/library?limit=1&source=studio').then(
    () => true,
    (e: unknown) => {
      library = null
      return e instanceof Refusal && e.status !== 404 && e.status !== 405 ? Promise.reject(e) : false
    },
  )
  return library.catch(() => false)
}

/** The most a browser may send the Library in one save (its inline limit). */
const INLINE = 10 * 1024 * 1024

const KINDS_LIB: Record<string, string> = { image: 'image', poster: 'image', ad: 'image', video: 'video', voice: 'audio', music: 'audio', document: 'document', slides: 'slides', type: 'type', mesh: 'model' }

/** Whether a key and its bytes belong in the Library: a made file the Library can take from a browser. */
const shelvable = (key: string, blob: Blob) => key.startsWith('files/') && key.split('/').length > 3 && blob.size <= INLINE

const base64 = async (b: Blob) => {
  const buf = new Uint8Array(await b.arrayBuffer())
  let bin = ''
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
  return btoa(bin)
}

/** Save one made file to the Library. Answers its item; the key makes a repeat the same item. */
async function shelve(w: Wire, key: string, blob: Blob): Promise<string> {
  const [, kind, chat] = key.split('/')
  const item = await ask<{ id?: string }>(w, 'api', '/v1/library', {
    body: {
      name: key.split('/').pop(),
      mime: blob.type || 'application/octet-stream',
      data: await base64(blob),
      source: 'studio',
      kind: KINDS_LIB[kind!] ?? 'file',
      ref: chat,
      origin: `/studio/c/${chat}`,
      key,
    },
  })
  if (!item.id) throw new Refusal(502, 'The Library took the file and named no item.')
  return item.id
}

/** What this page made or read, held for its life: a file is never fetched twice. */
const held = new Map<string, Blob>()

/** Keep bytes at a key: a made file in the Library, anything else in this browser. Answers the key. */
export async function put(w: Wire, key: string, blob: Blob): Promise<string> {
  const r: Record_ = { key, mime: blob.type || 'application/octet-stream', size: blob.size, modified: Date.now() }
  const lib = shelvable(key, blob) && (await libraryLive(w)) ? await shelve(w, key, blob).catch(() => undefined) : undefined
  await keep(w, lib ? { ...r, lib } : { ...r, blob })
  held.set(key, blob)
  forget(key)
  return key
}

/**
 * Carry up to the Library every made file this browser still holds the bytes
 * of, and drop them here once the Library has them. Answers how many went.
 */
export async function sync(w: Wire): Promise<number> {
  if (!(await libraryLive(w))) return 0
  let n = 0
  for (const r of await all(w)) {
    if (!r.blob || !shelvable(r.key, r.blob)) continue
    const lib = r.lib ?? (await shelve(w, r.key, r.blob).catch(() => undefined))
    if (!lib) continue
    const { blob, ...rest } = r
    await keep(w, { ...rest, size: blob.size, lib })
    n++
  }
  return n
}

/** Addresses handed out, kept while they work: an object URL for the page's life, a Library URL until it expires. */
const shown = new Map<string, { url: string; until: number }>()

function forget(key: string) {
  const hit = shown.get(key)
  if (hit?.url.startsWith('blob:')) URL.revokeObjectURL(hit.url)
  shown.delete(key)
}

/** An address a page can draw a kept file at. */
export async function url(w: Wire, key: string): Promise<string> {
  const hit = shown.get(key)
  if (hit && hit.until > Date.now()) return hit.url
  const local = held.get(key) ?? (await get(w, key))?.blob
  if (local) {
    const made = URL.createObjectURL(local)
    shown.set(key, { url: made, until: Number.POSITIVE_INFINITY })
    return made
  }
  const r = await get(w, key)
  if (!r?.lib) throw new Refusal(404, 'This file is not in your Library.')
  const item = await ask<{ url?: string; expires?: string }>(w, 'api', `/v1/library/${enc(r.lib)}`)
  if (!item.url) throw new Refusal(502, 'The Library named no address for the file.')
  shown.set(key, { url: item.url, until: item.expires ? Date.parse(item.expires) - 60_000 : Date.now() + 240_000 })
  return item.url
}

/** A kept file's bytes: from this page, this browser, or the Library. */
export async function read(w: Wire, key: string): Promise<Blob> {
  const hit = held.get(key)
  if (hit) return hit
  const r = await get(w, key)
  if (!r) throw new Refusal(404, 'This file is not in your Library.')
  if (r.blob) return r.blob
  const res = await fetch(await url(w, key), { cache: 'no-store', credentials: 'omit' })
  if (!res.ok) throw new Refusal(res.status, 'The Library would not hand the file back.')
  const b = await res.blob()
  held.set(key, b)
  return b
}

export interface Entry {
  /** Relative to the prefix listed. */
  key: string
  size: number
  /** Unix milliseconds. */
  modified: number
}

/** What is kept under a prefix (one that ends in '/'), keys relative to it. */
export async function list(w: Wire, prefix: string): Promise<Entry[]> {
  const range = IDBKeyRange.bound(prefix, `${prefix}￿`)
  const rows = await tx<Record_[]>(await db(w), 'readonly', (s) => s.getAll(range))
  return rows.map((r) => ({ key: r.key.slice(prefix.length), size: r.size ?? r.blob?.size ?? 0, modified: r.modified }))
}

/** Delete a kept file: its Library item and bytes first, then this browser's record. */
export async function remove(w: Wire, key: string): Promise<void> {
  const r = await get(w, key)
  if (r?.lib) await unshelve(w, r.lib)
  await tx(await db(w), 'readwrite', (s) => s.delete(key))
  held.delete(key)
  forget(key)
}

/** Delete one Library item. One already gone is gone. */
async function unshelve(w: Wire, id: string): Promise<void> {
  await ask(w, 'api', `/v1/library/${enc(id)}`, { method: 'DELETE' }).catch((e: unknown) => {
    if (!(e instanceof Refusal && e.status === 404)) throw e
  })
}

/** A kept JSON document, or null when there is none. */
export async function load<T>(w: Wire, key: string): Promise<T | null> {
  const r = await get(w, key)
  if (!r?.blob) return null
  try {
    return JSON.parse(await r.blob.text()) as T
  } catch {
    return null
  }
}

export const save = (w: Wire, key: string, value: unknown): Promise<string> =>
  put(w, key, new Blob([JSON.stringify(value)], { type: 'application/json' }))

/* The Library as the person reads it: every Studio item they made, newest first. */

export interface Shelf {
  id: string
  kind: string
  name: string
  mime: string
  bytes: number
  ref: string
  origin: string
  url: string
  download: string
  preview: string
  created: string
  shared: boolean
}

/** Every Studio item in the person's Library, read a page at a time. */
export async function shelf(w: Wire, signal?: AbortSignal): Promise<Shelf[]> {
  const out: Shelf[] = []
  let cursor = ''
  do {
    const page = await ask<{ data?: Shelf[]; next?: string }>(w, 'api', `/v1/library${query({ source: 'studio', scope: 'mine', limit: 200, cursor })}`, { signal })
    out.push(...(page.data ?? []))
    cursor = page.next ?? ''
  } while (cursor)
  return out
}

/** Delete one item from the Library, and this browser's record of it. */
export async function discard(w: Wire, id: string): Promise<void> {
  await unshelve(w, id)
  for (const r of await all(w)) if (r.lib === id) await tx(await db(w), 'readwrite', (s) => s.delete(r.key))
}

/* ------------------------------------------------------------------ engine */

export interface Node {
  identity: string
  name?: string
  online?: boolean
  /** The lanes this GPU can run now: every file its graph loads is present. */
  lanes?: string[]
}

export interface Fleet {
  state: 'busy' | 'ready' | 'offline'
  online: number
  total: number
  nodes: Node[]
}

/** The org's GPUs and what each can run. A fleet the engine cannot read is an error, never an empty fleet. */
export const gpus = (w: Wire, signal?: AbortSignal) => ask<Fleet>(w, 'engine', '/v1/gpu-status', { signal })

export interface Submitted {
  prompt_id?: string
  id?: string
  node?: string | null
  output?: string
  outputs?: string[]
}

/** Dispatch a lane to the org's GPUs (the engine's one funnel). */
export const dispatch = (w: Wire, lane: 'image' | 'video' | 'fix' | 'model3d' | 'music', body: object, signal?: AbortSignal) =>
  ask<Submitted>(w, 'engine', `/v1/library/${lane}`, { body, signal })

export interface Row {
  id: string
  status: string
  kind?: string
  output?: string
  outputs?: string[]
  error?: string
  prompt_id?: string
}

/** The engine's record of what was dispatched and what landed. */
export const worklog = (w: Wire, signal?: AbortSignal) => ask<{ items: Row[] }>(w, 'engine', '/v1/worklog', { signal }).then((r) => r.items ?? [])

/** A landed file of the engine library, as bytes. */
export const landed = (w: Wire, path: string) => ask<Blob>(w, 'engine', `/v1/library/file${query({ path })}`)

/** Documents and decks: the engine's renderers. */
export const documents = {
  schema: (w: Wire) => ask<object>(w, 'engine', '/v1/documents/schema'),
  create: (w: Wire, body: { kind: string; ir: object; title?: string }) => ask<{ id: string }>(w, 'engine', '/v1/documents', { body }),
  generate: (w: Wire, body: { templateId: string; description?: string; fields?: object }) => ask<{ id: string }>(w, 'engine', '/v1/documents/generate', { body }),
  patch: (w: Wire, id: string, body: object) => ask<{ id: string }>(w, 'engine', `/v1/documents/${enc(id)}`, { method: 'PATCH', body }),
  get: (w: Wire, id: string) => ask<{ id: string; kind: string; ir: object; title: string }>(w, 'engine', `/v1/documents/${enc(id)}`),
  file: (w: Wire, id: string, format: string) => ask<Blob>(w, 'engine', `/v1/documents/${enc(id)}/download${query({ format })}`),
}

/** Stage a picture in the engine's input folder for a lane that starts from one. Answers the name the lane takes. */
export async function stage(w: Wire, blob: Blob, name: string): Promise<string> {
  const form = new FormData()
  form.append('image', blob, name)
  form.append('type', 'input')
  form.append('overwrite', 'false')
  const r = await ask<{ name: string; subfolder?: string }>(w, 'engine', '/v1/upload/image', { raw: form })
  return r.subfolder ? `${r.subfolder}/${r.name}` : r.name
}

/** A clip from a gateway video model: started, followed, read back (the async /v1/videos API). */
export async function film(w: Wire, model: string, prompt: string, o: { size?: string; seconds?: number; doing: (l: string) => void; signal: AbortSignal }): Promise<Blob> {
  const job = await ask<{ id?: string; data?: { id?: string }[] }>(w, 'api', '/v1/videos/generations', { body: { model, prompt, size: o.size, seconds: o.seconds }, signal: o.signal })
  const id = job.id ?? job.data?.[0]?.id
  if (!id) throw new Refusal(502, `${model} took the clip but named no job.`)
  const until = Date.now() + 20 * 60_000
  for (;;) {
    const s = await ask<{ status?: string; error?: string | { message?: string } }>(w, 'api', `/v1/videos/${enc(id)}`, { signal: o.signal })
    const status = (s.status ?? '').toLowerCase()
    if (['completed', 'succeeded', 'done'].includes(status)) break
    if (['failed', 'error', 'cancelled'].includes(status)) throw new Refusal(502, typeof s.error === 'string' ? s.error : (s.error?.message ?? `The clip ${status}.`))
    if (Date.now() > until) throw new Refusal(504, 'The clip is still rendering.')
    o.doing(status === 'queued' ? 'Waiting for the video model' : 'Rendering the clip')
    await new Promise((r) => setTimeout(r, 4000))
  }
  return ask<Blob>(w, 'api', `/v1/videos/${enc(id)}/content`, { signal: o.signal })
}

/** The gateway's catalog of models that make something other than words. */
export async function makers(w: Wire, signal?: AbortSignal): Promise<{ id: string; name: string; inputs: string[]; outputs: string[] }[]> {
  const out = await ask<{ data?: { id: string; name?: string; inputs?: string[]; outputs?: string[] }[] }>(w, 'api', '/v1/models', { signal })
  return (out.data ?? [])
    .filter((m) => (m.outputs ?? []).some((o) => ['image', 'audio', 'video', '3d', 'mesh'].includes(o)))
    .map((m) => ({ id: m.id, name: m.name ?? m.id, inputs: m.inputs ?? [], outputs: m.outputs ?? [] }))
}
