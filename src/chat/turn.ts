/**
 * ONE TURN of the conversation: the person says what to make, the chat calls
 * a kind's tool, the kind makes it, and it opens as a tab.
 *
 * The chat model (api.hanzo.ai) is handed every kind that can run now as a
 * tool, plus `revise` (change a tab), and the list of open tabs by number, so
 * "make the poster in tab 2 warmer" is a call to revise {tab: 2}. Each call is
 * run here, in order; its result goes back to the model as the tool's answer,
 * and the model closes the turn with a line of its own. A kind that cannot run
 * now (no GPU for 3D) is not offered as a tool; the model is told why, so it
 * says so instead of pretending.
 *
 * Nothing a turn makes is held only in memory: each version's files are
 * stored as they are made, and the conversation is saved after every change.
 */
import { complete, put, read, type Message, type Tool as Wired, type Wire } from '../wire.ts'
import { ALL, called, kind } from '../kinds/index.ts'
import { UNDRAWN } from '../kinds/image.tsx'
import type { Available, Draft, Kind, Work } from '../kinds/kind.ts'
import { settle, type TypeSystem } from '../kinds/type/system.ts'
import { current, dir, type Chat, type KindId, type Made, type Stored, type Turn, type Version } from '../made.ts'
import { data } from '../raster.ts'
import { fresh } from '../route.ts'

export interface Asked {
  text: string
  attached: Stored[]
  /** The kind the person picked (a pill, the Kind chip); none for Auto. */
  kind?: KindId
  /** Lane and shape choices from the composer's options. */
  options?: { model?: string; aspect?: string; count?: number }
}

export interface Context extends Available {
  wire: Wire
  /** The model the chat runs on: it calls the tools. */
  brain: string
  /** The model a kind writes words with. */
  writer: string
  /** The tabs, in their order: made ids. */
  order: () => string[]
  /** Open a made thing's tab and show it. */
  show: (id: string) => void
  /** The conversation changed: draw it, and keep it. */
  changed: (chat: Chat) => void
  signal: AbortSignal
}

const ROUNDS = 4

const id = () => fresh().slice(-12)

const REVISE: Wired = {
  type: 'function',
  function: {
    name: 'revise',
    description: 'Change something already made, by its tab number: "warmer", "shorter headline", "make it a story", "add a pricing slide". Makes a new version in the same tab.',
    parameters: {
      type: 'object',
      properties: {
        tab: { type: 'integer', description: 'The tab number, as listed.' },
        instruction: { type: 'string', description: 'What to change, in full.' },
      },
      required: ['tab', 'instruction'],
    },
  },
}

const BRAND: Wired = {
  type: 'function',
  function: {
    name: 'use_brand',
    description: 'Draw what is made next in the type system in a tab (a brand), or in none.',
    parameters: { type: 'object', properties: { tab: { type: 'integer', description: 'A type system tab, or 0 for none.' } }, required: ['tab'] },
  },
}

/** The brand a conversation draws in, read from its type system. */
export function brandOf(chat: Chat): TypeSystem | null {
  if (!chat.brand) return null
  for (const m of chat.made) for (const v of m.versions) if (m.kind === 'type' && (m.id === chat.brand || v.id === chat.brand)) return settle(current(m).spec as Partial<TypeSystem>)
  return null
}

function system(chat: Chat, ctx: Context, open: Kind[], closed: Kind[]): string {
  const order = ctx.order()
  const tabs = order
    .map((mid, i) => {
      const m = chat.made.find((x) => x.id === mid)
      return m ? `${i + 1}. ${kind(m.kind).label} "${m.title}" (version ${m.at + 1} of ${m.versions.length}${m.id === chat.brand ? ', the brand' : ''})` : ''
    })
    .filter(Boolean)
  const brand = brandOf(chat)
  return [
    'You are Hanzo Studio: a creative studio in a chat. You make things by calling tools, and everything you make opens in a numbered tab beside this conversation.',
    'When the person asks for something, call the tool that makes it at once, with complete, specific, well-written arguments: real words, not placeholders. Ask a question only when you cannot make anything sensible without the answer.',
    'To change something already made, call revise with its tab number. "The poster in tab 2" is tab 2.',
    'After the tools have run, answer in one or two short sentences: what you made, and one useful next step. Never paste file contents, links or JSON. If a tool answers with an error, say plainly what went wrong.',
    brand ? `Brand: ${brand.name} (${brand.display} / ${brand.text}; ${brand.blurb}). Posters, documents, slides and ads are drawn in it.` : 'No brand is chosen: things are drawn in the Hanzo type system unless the person asks for a type system.',
    tabs.length ? `Tabs:\n${tabs.join('\n')}` : 'No tabs are open yet.',
    closed.length ? `Not available right now: ${closed.map((k) => `${k.label} (${k.needs(ctx)})`).join('; ')}.` : '',
    `You can make: ${open.map((k) => k.label).join(', ')}.`,
  ]
    .filter(Boolean)
    .join('\n\n')
}

/** The conversation so far, as the model reads it: words, and what each turn made. */
function history(chat: Chat, ctx: Context): Message[] {
  const order = ctx.order()
  return chat.turns
    .filter((t) => t.text || t.made?.length)
    .map((t): Message => {
      if (t.role === 'user') {
        const pics = t.attached?.length ? `\n[Attached ${t.attached.length} picture${t.attached.length > 1 ? 's' : ''}, now in tab ${t.made?.map((m) => order.indexOf(m) + 1).join(', ')}]` : ''
        return { role: 'user', content: t.text + pics }
      }
      const made = (t.made ?? [])
        .map((mid) => chat.made.find((m) => m.id === mid))
        .filter((m): m is Made => Boolean(m))
        .map((m) => `[tab ${order.indexOf(m.id) + 1}: ${kind(m.kind).label} "${m.title}"]`)
        .join(' ')
      return { role: 'assistant', content: [t.text, made].filter(Boolean).join(' ') || '…' }
    })
}

const words = (e: unknown): string => (e instanceof Error ? e.message : String(e))

/** Run one turn. Answers the conversation as it stands at the end. */
export async function turn(start: Chat, asked: Asked, ctx: Context): Promise<Chat> {
  let chat: Chat = start
  const put_ = (next: Chat) => {
    chat = { ...next, updated: Date.now() }
    ctx.changed(chat)
  }
  const me: Turn = { id: id(), role: 'user', text: asked.text, at: Date.now(), attached: asked.attached.length ? asked.attached : undefined }
  const you: Turn = { id: id(), role: 'assistant', text: '', at: Date.now(), doing: 'Thinking', made: [] }

  // An attached picture is a thing in the conversation: its own tab, so it can be named.
  const uploads: Made[] = asked.attached.map((f) => ({
    id: id(),
    kind: 'image',
    title: f.name.replace(/\.[a-z0-9]+$/i, '') || 'Picture',
    at: 0,
    versions: [{ id: id(), at: Date.now(), spec: { prompt: '', uploaded: true }, files: [f], note: 'Attached', by: 'you' }],
  }))
  me.made = uploads.map((u) => u.id)
  put_({
    ...chat,
    title: chat.turns.length ? chat.title : asked.text.slice(0, 60) || chat.title,
    turns: [...chat.turns, me, you],
    made: [...chat.made, ...uploads],
  })
  for (const u of uploads) ctx.show(u.id)

  const set = (patch: Partial<Turn>) => {
    put_({ ...chat, turns: chat.turns.map((t) => (t.id === you.id ? { ...t, ...patch } : t)) })
  }
  const mine = () => chat.turns.find((t) => t.id === you.id)!
  // A picture the gateway did not draw: the turn ends on one plain sentence and a retry.
  let missed = false

  const open = ALL.filter((k) => !k.needs(ctx))
  const closed = ALL.filter((k) => k.needs(ctx))
  const tools: Wired[] = [...open.map((k) => ({ type: 'function' as const, function: k.tool })), REVISE, BRAND]
  const messages: Message[] = [{ role: 'system', content: system(chat, ctx, open, closed) }, ...history(start, ctx), { role: 'user', content: me.text + (uploads.length ? `\n[Attached: tab ${uploads.map((u) => ctx.order().indexOf(u.id) + 1).join(', ')}]` : '') }]
  const picked = asked.kind ? kind(asked.kind) : null
  if (picked && picked.needs(ctx)) {
    set({ doing: undefined, text: `${picked.label} can't run right now. ${picked.needs(ctx)}` })
    return chat
  }
  if (picked) messages.push({ role: 'system', content: `The person chose ${picked.label}: answer by calling ${picked.tool.name}.` })

  try {
    for (let round = 0; round < ROUNDS; round++) {
      const said = await complete(
        ctx.wire,
        { model: ctx.brain, messages, tools },
        (text) => set({ text, doing: text ? undefined : mine().doing }),
        ctx.signal,
      )
      set({ text: said.text, by: said.model, doing: said.calls.length ? 'Working' : undefined })
      if (!said.calls.length) break
      messages.push({ role: 'assistant', content: said.text, tool_calls: said.calls.map((c) => ({ id: c.id, type: 'function', function: { name: c.name, arguments: c.args } })) })
      for (const c of said.calls) {
        let args: Record<string, unknown> = {}
        try {
          args = c.args ? (JSON.parse(c.args) as Record<string, unknown>) : {}
        } catch {
          messages.push({ role: 'tool', tool_call_id: c.id, content: JSON.stringify({ ok: false, error: 'The arguments were not JSON.' }) })
          continue
        }
        const answer = await call(c.name, args)
        messages.push({ role: 'tool', tool_call_id: c.id, content: JSON.stringify(answer) })
      }
    }
    set({ doing: undefined, ...(missed ? { failed: `${UNDRAWN} Try again?` } : {}) })
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') set({ doing: undefined, failed: 'Stopped.' })
    else set({ doing: undefined, failed: words(e) })
  }
  return chat

  async function call(name: string, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (name === 'use_brand') {
      const tabN = Number(args.tab) || 0
      const m = tabN ? chat.made.find((x) => x.id === ctx.order()[tabN - 1]) : null
      if (tabN && (!m || m.kind !== 'type')) return { ok: false, error: `Tab ${tabN} is not a type system.` }
      put_({ ...chat, brand: m?.id })
      return { ok: true, brand: m?.title ?? 'none' }
    }
    if (name === 'revise') {
      const tabN = Number(args.tab) || 0
      const m = chat.made.find((x) => x.id === ctx.order()[tabN - 1])
      if (!m) return { ok: false, error: `There is no tab ${tabN}.` }
      const k = kind(m.kind)
      const why = k.needs(ctx)
      if (why) return { ok: false, error: why }
      set({ doing: `Revising ${m.title}` })
      try {
        const v = await version(m, (w) => k.revise(w, m, { instruction: String(args.instruction ?? '') }))
        return { ok: true, tab: tabN, title: m.title, kind: k.label, version: v, note: chat.made.find((x) => x.id === m.id)?.versions.at(-1)?.note }
      } catch (e) {
        return { ok: false, error: words(e) }
      }
    }
    const k = called(name)
    if (!k) return { ok: false, error: `There is no tool called ${name}.` }
    const why = k.needs(ctx)
    if (why) return { ok: false, error: why }
    const opts = asked.options ?? {}
    const merged = { ...args, ...(k.id === 'image' && opts.model ? { model: opts.model } : {}), ...(k.id === 'image' && opts.aspect && !args.aspect ? { aspect: opts.aspect } : {}), ...(k.id === 'image' && opts.count ? { count: opts.count } : {}) }
    const m: Made = { id: id(), kind: k.id, title: String(args.title ?? k.label), versions: [], at: 0 }
    set({ doing: `Making ${k.label.toLowerCase()}` })
    try {
      const vid = id()
      const made = await k.make(work(m, vid), merged)
      const v: Version = { ...made.draft, id: vid, at: Date.now() }
      const done: Made = { ...m, title: made.title, versions: [v], at: 0 }
      const brand = k.id === 'type' && !chat.brand ? done.id : chat.brand
      put_({ ...chat, brand, made: [...chat.made, done], turns: chat.turns.map((t) => (t.id === you.id ? { ...t, made: [...(t.made ?? []), done.id] } : t)) })
      ctx.show(done.id)
      return { ok: true, tab: ctx.order().indexOf(done.id) + 1, title: done.title, kind: k.label, note: v.note }
    } catch (e) {
      return { ok: false, error: words(e) }
    }
  }

  /** A new version of `m`, made by `run`, stored and shown. */
  async function version(m: Made, run: (w: Work) => Promise<Draft>): Promise<number> {
    const vid = id()
    const draft = await run(work(m, vid))
    const v: Version = { ...draft, id: vid, at: Date.now() }
    const now = chat.made.find((x) => x.id === m.id) ?? m
    const next: Made = { ...now, versions: [...now.versions, v], at: now.versions.length }
    put_({ ...chat, made: chat.made.map((x) => (x.id === m.id ? next : x)), turns: chat.turns.map((t) => (t.id === you.id ? { ...t, made: [...new Set([...(t.made ?? []), m.id])] } : t)) })
    ctx.show(m.id)
    return next.versions.length
  }

  function work(m: Pick<Made, 'id' | 'kind'>, vid: string): Work {
    return {
      wire: ctx.wire,
      signal: ctx.signal,
      doing: (line) => set({ doing: line }),
      missed: () => {
        missed = true
      },
      keep: async (name, blob, extra) => {
        const key = await put(ctx.wire, `${dir(chat.id, m, vid)}/${name}`, blob)
        return { key, mime: blob.type || 'application/octet-stream', name, ...extra }
      },
      picture: async (f) => data(await read(ctx.wire, f.key)),
      find: (ref) => {
        const order = ctx.order()
        const mid = typeof ref === 'number' ? order[ref - 1] : ref
        return chat.made.find((x) => x.id === mid) ?? null
      },
      brand: brandOf(chat),
      fleet: ctx.fleet,
      models: ctx.models,
      writer: ctx.writer,
    }
  }
}

/** An edit in place: a new version of one made thing from a patch to its spec, with no chat turn. */
export async function edit(start: Chat, m: Made, patch: Record<string, unknown>, note: string, ctx: Omit<Context, 'show' | 'brain'>): Promise<Chat> {
  const k = kind(m.kind)
  const vid = id()
  const w: Work = {
    wire: ctx.wire,
    signal: ctx.signal,
    doing: () => undefined,
    missed: () => undefined,
    keep: async (name, blob, extra) => ({ key: await put(ctx.wire, `${dir(start.id, m, vid)}/${name}`, blob), mime: blob.type || 'application/octet-stream', name, ...extra }),
    picture: async (f) => data(await read(ctx.wire, f.key)),
    find: (ref) => start.made.find((x) => x.id === (typeof ref === 'number' ? ctx.order()[ref - 1] : ref)) ?? null,
    brand: brandOf(start),
    fleet: ctx.fleet,
    models: ctx.models,
    writer: ctx.writer,
  }
  const draft = await k.revise(w, m, { patch })
  const v: Version = { ...draft, note, id: vid, at: Date.now() }
  const next: Made = { ...m, versions: [...m.versions, v], at: m.versions.length }
  const chat = { ...start, updated: Date.now(), made: start.made.map((x) => (x.id === m.id ? next : x)) }
  ctx.changed(chat)
  return chat
}
