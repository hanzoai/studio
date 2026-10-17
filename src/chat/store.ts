/**
 * Conversations as stored, and the list of them.
 *
 *   chats/<id>.json     one conversation, whole
 *   chats/index.json    every conversation's id, title, time, state and
 *                              the picture to show for it: the list Recent and
 *                              the rail read without opening each one
 *
 * The index is rewritten on every save of a conversation; a lost race between
 * two tabs loses only a row's freshness, and `rebuild` lists the folder to
 * write it again from the conversations themselves.
 */
import { current, type Chat } from '../made.ts'
import { list, load, save } from '../wire.ts'
import type { Wire } from '../wire.ts'

export interface Listed {
  id: string
  title: string
  updated: number
  /** making while a turn works, failed when the last turn failed, else done. */
  state: 'making' | 'done' | 'failed'
  /** A stored picture to show for it. */
  cover?: string
  /** How many things it made. */
  count: number
}

const INDEX = 'chats/index.json'

export function listed(c: Chat): Listed {
  const last = c.turns.at(-1)
  const state = last?.doing ? 'making' : last?.failed ? 'failed' : 'done'
  let cover: string | undefined
  for (const m of [...c.made].reverse()) {
    const f = current(m).files.find((x) => x.mime.startsWith('image/'))
    if (f) {
      cover = f.key
      break
    }
  }
  return { id: c.id, title: c.title, updated: c.updated, state, cover, count: c.made.length }
}

export async function index(w: Wire): Promise<Listed[]> {
  const rows = (await load<Listed[]>(w, INDEX)) ?? []
  return rows.sort((a, b) => b.updated - a.updated)
}

/** Save a conversation, and its row in the index. */
export async function keep(w: Wire, c: Chat): Promise<void> {
  await save(w, `chats/${c.id}.json`, c)
  const rows = (await load<Listed[]>(w, INDEX)) ?? []
  const next = [listed(c), ...rows.filter((r) => r.id !== c.id)].slice(0, 500)
  await save(w, INDEX, next)
}

export const open = (w: Wire, id: string) => load<Chat>(w, `chats/${id}.json`)

/** Write the index again from the conversations stored. */
export async function rebuild(w: Wire): Promise<Listed[]> {
  const entries = (await list(w, 'chats/')).filter((e) => e.key.endsWith('.json') && e.key !== 'index.json')
  const rows: Listed[] = []
  for (const e of entries) {
    const c = await open(w, e.key.replace(/\.json$/, ''))
    if (c) rows.push(listed(c))
  }
  await save(w, INDEX, rows)
  return rows.sort((a, b) => b.updated - a.updated)
}

export async function forget(w: Wire, id: string): Promise<void> {
  const rows = (await load<Listed[]>(w, INDEX)) ?? []
  await save(w, INDEX, rows.filter((r) => r.id !== id))
}
