/**
 * A KIND is one thing Studio makes: an image, a poster, a type system, a deck.
 * Each is the same five parts, so the chat, the tabs and the library treat
 * every kind one way:
 *
 *   tool      what the chat calls to make one (a function the model may call)
 *   needs     why it cannot run right now, or null: a GPU that is not online,
 *             a lane that is not served. The home shows only kinds that can run.
 *   make      a first version from the tool's arguments
 *   revise    a new version from the shown one and an instruction, or a patch
 *             to its spec (an edit in place)
 *   View      the tab: one version drawn; `Edit` the controls an edit in place
 *             offers, beside it
 *   exports   the files a person takes away
 *
 * A kind never touches the network itself. It is handed a `Work`, which stores
 * what it makes under the version's own folder and reads what it needs, all
 * through the one client module (wire.ts).
 */
import type { ComponentType } from 'react'

import type { KindId, Made, Stored, Version } from '../made.ts'
import type { TypeSystem } from './type/system.ts'
import type { Fleet, Wire } from '../wire.ts'

/** A version as a kind produces it: the id and time are the conversation's to give. */
export type Draft = Omit<Version, 'id' | 'at'>

export interface Work {
  wire: Wire
  signal: AbortSignal
  /** Say what is happening now: drawn under the turn while it works. */
  doing: (line: string) => void
  /** Keep bytes as one of this version's files. */
  keep: (name: string, blob: Blob, extra?: { width?: number; height?: number }) => Promise<Stored>
  /** A stored picture as a data: URL, for a model to start from. */
  picture: (f: Stored) => Promise<string>
  /** Another thing this conversation made, by its tab number (1-based) or id. */
  find: (ref: number | string) => Made | null
  /** The type system things are drawn in, when one is chosen. */
  brand: TypeSystem | null
  /** The org's GPUs and their lanes, as last read; null when unread. */
  fleet: Fleet | null
  /** The gateway's models that make something other than words. */
  models: readonly Served[]
  /** The model the chat itself runs on, for a kind that writes words. */
  writer: string
}

/** A gateway model, as the catalog lists it: what it takes and what it makes. */
export interface Served {
  id: string
  name: string
  inputs: readonly string[]
  outputs: readonly string[]
}

/** What can run right now: the org's GPUs and the gateway's models. */
export interface Available {
  fleet: Fleet | null
  models: readonly Served[]
}

/** The first gateway model that makes `output`. */
export const maker = (a: Pick<Available, 'models'>, output: string): Served | null => a.models.find((m) => m.outputs.includes(output)) ?? null

export interface Tool {
  name: string
  description: string
  /** A JSON Schema for the arguments. */
  parameters: object
}

export interface ViewProps {
  made: Made
  version: Version
  wire: Wire
  /** Make a new version from a patch to this one's spec: an edit in place. */
  edit: (patch: Record<string, unknown>, note: string) => void
  busy: boolean
  /** Everything else this conversation made, in tab order: a poster takes its picture from one. */
  others: readonly Made[]
}

export interface Export {
  label: string
  /** The file's name, from the title. */
  run: (wire: Wire, made: Made, version: Version) => Promise<{ name: string; blob: Blob }>
}

export interface Kind {
  id: KindId
  label: string
  /** One line: what it makes. */
  blurb: string
  icon: ComponentType<{ size?: number }>
  tool: Tool
  needs: (a: Available) => string | null
  make: (w: Work, args: Record<string, unknown>) => Promise<{ title: string; draft: Draft }>
  revise: (w: Work, made: Made, change: { instruction?: string; patch?: Record<string, unknown> }) => Promise<Draft>
  View: ComponentType<ViewProps>
  exports: Export[]
}

/** Whether an online GPU of the org advertises `lane`. */
export function lit(fleet: Fleet | null, lane: string): boolean {
  return Boolean(fleet?.nodes.some((n) => n.online !== false && n.lanes?.includes(lane)))
}

/** A string argument, trimmed, or `or`. */
export const str = (v: unknown, or = ''): string => (typeof v === 'string' && v.trim() ? v.trim() : or)

/** A number argument within bounds, or `or`. */
export const num = (v: unknown, or: number, lo = -Infinity, hi = Infinity): number => {
  const n = typeof v === 'number' ? v : typeof v === 'string' ? Number(v) : NaN
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : or
}
