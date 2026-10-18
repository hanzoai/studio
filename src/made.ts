/**
 * What a conversation is, and what it makes, as kept.
 *
 * A conversation is one JSON document at `chats/<id>.json`, kept for this
 * person alone (wire.ts): its turns, and every thing it made. A thing made has
 * versions; a version is the spec it was made from (the kind's own fields, so
 * an edit is a new spec) and the files it produced, each kept at
 *
 *   files/<kind>/<chat>/<made>/<version>/<name>
 *
 * so the library is one listing of `files/`, sorted by kind, and a file names
 * the conversation that made it.
 */
import { fresh } from './route.ts'

export type KindId = 'image' | 'poster' | 'document' | 'slides' | 'type' | 'mesh' | 'video' | 'voice' | 'music' | 'ad'

export interface Stored {
  /** The full key in the bucket. */
  key: string
  mime: string
  name: string
  width?: number
  height?: number
}

export interface Version {
  id: string
  /** Unix milliseconds. */
  at: number
  /** The kind's own fields this version was made from. */
  spec: Record<string, unknown>
  files: Stored[]
  /** What made it, in a line: the ask, or the edit. */
  note: string
  /** The model or lane that made it, as it named itself. */
  by?: string
}

export interface Made {
  id: string
  kind: KindId
  title: string
  versions: Version[]
  /** The version shown: an index into `versions`. */
  at: number
}

export interface Turn {
  id: string
  role: 'user' | 'assistant'
  text: string
  at: number
  /** Things this turn made or changed, by id. */
  made?: string[]
  /** What it is doing now, while it works. */
  doing?: string
  /** Why it stopped, when it failed. */
  failed?: string
  /** The model that answered. */
  by?: string
  /** Pictures the person attached: stored files. */
  attached?: Stored[]
}

export interface Chat {
  id: string
  title: string
  created: number
  updated: number
  turns: Turn[]
  made: Made[]
  /** The type system every kind is drawn in: a `type` made's file key, or none. */
  brand?: string
}

export const blank = (id = fresh()): Chat => ({ id, title: 'New creation', created: Date.now(), updated: Date.now(), turns: [], made: [] })

/** The version a made thing shows. */
export const current = (m: Made): Version => m.versions[Math.min(Math.max(0, m.at), m.versions.length - 1)]!

/** Where a version's files live. */
export const dir = (chat: string, m: Pick<Made, 'id' | 'kind'>, version: string) => `files/${m.kind}/${chat}/${m.id}/${version}`

/** A file name a person reads: the title, slugged, and the extension. */
export function filename(title: string, ext: string): string {
  const base = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60)
  return `${base || 'untitled'}.${ext}`
}

