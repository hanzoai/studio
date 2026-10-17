/**
 * What Studio reads while it is open: the call target from the host, what can
 * run (the org's GPUs and the gateway's makers), and the list of conversations.
 * One reader each, shared by every pane, so the home, the composer and the
 * tools agree about what is on offer.
 */
import type { Host } from '@hanzo/build'
import { useEffect, useMemo, useState, useSyncExternalStore } from 'react'

import { index, type Listed } from './chat/store.ts'
import type { Available, Served } from './kinds/kind.ts'
import { ENGINE, gpus, libraryLive, makers, sync, type Fleet, type Wire } from './wire.ts'

/**
 * Where the engine is from this page: this origin on studio.hanzo.ai, a dev
 * server's /engine proxy on a laptop, studio.hanzo.ai from anywhere else.
 */
export function engineFrom(hostname: string): string {
  if (hostname === 'studio.hanzo.ai') return ''
  if (hostname === 'localhost' || hostname === '127.0.0.1') return '/engine'
  return ENGINE
}

/**
 * The models Studio's own words come from. The chat that decides what to make
 * calls tools; the writer answers JSON a kind asks for (a type system, a deck).
 * Measured 2026-10-16 on api.hanzo.ai: zen5 calls tools in ~3 s and zen5-flash
 * answers a strict schema in ~2 s, where zen5 spent 30 s reasoning over the same
 * schema and zen5-flash answered tools in prose.
 */
export const BRAIN = 'zen5'
export const WRITER = 'zen5-flash'

export function useWire(host: Host): Wire {
  const engine = typeof window === 'undefined' ? ENGINE : engineFrom(window.location.hostname)
  return useMemo(() => ({ api: host.api, engine, token: host.token, org: host.org }), [host.api, host.token, host.org, engine])
}

/* One shared read of what can run, refreshed while anyone is looking. */

let state: Available & { error: string } = { fleet: null, models: [], error: '' }
const listeners = new Set<() => void>()
let users = 0
let timer: ReturnType<typeof setInterval> | undefined
let target: Wire | null = null

const tell = () => listeners.forEach((l) => l())

async function poll(): Promise<void> {
  const w = target
  if (!w || !w.token()) return
  const [fleet, models] = await Promise.allSettled([gpus(w), makers(w)])
  state = {
    fleet: fleet.status === 'fulfilled' ? fleet.value : state.fleet,
    models: models.status === 'fulfilled' ? (models.value as Served[]) : state.models,
    error: fleet.status === 'rejected' ? (fleet.reason instanceof Error ? fleet.reason.message : String(fleet.reason)) : '',
  }
  tell()
}

/** The org's GPUs and the gateway's makers, read every 15 s while a Studio pane is drawn. */
export function useAvailable(w: Wire): Available & { error: string } {
  useEffect(() => {
    target = w
    users++
    void poll()
    if (users === 1) timer = setInterval(() => void poll(), 15_000)
    return () => {
      users--
      if (!users && timer) clearInterval(timer)
    }
  }, [w])
  return useSyncExternalStore(
    (l) => {
      listeners.add(l)
      return () => listeners.delete(l)
    },
    () => state,
    () => state,
  )
}

/* The conversation list, shared the same way. */

let rows: Listed[] = []
let rowsError = ''
const rowListeners = new Set<() => void>()
let reading: Promise<void> | null = null

export function reloadChats(w: Wire): Promise<void> {
  reading ??= index(w)
    .then(
      (r) => {
        rows = r
        rowsError = ''
      },
      (e: unknown) => {
        rowsError = e instanceof Error ? e.message : String(e)
      },
    )
    .finally(() => {
      reading = null
      rowListeners.forEach((l) => l())
    })
  return reading
}

/** A conversation's row changed here: shown at once, before the index is read again. */
export function touch(row: Listed): void {
  rows = [row, ...rows.filter((r) => r.id !== row.id)]
  rowListeners.forEach((l) => l())
}

export function useChats(w: Wire): { rows: Listed[]; error: string } {
  const [, bump] = useState(0)
  useEffect(() => {
    const l = () => bump((n) => n + 1)
    rowListeners.add(l)
    if (w.token()) void reloadChats(w)
    return () => {
      rowListeners.delete(l)
    }
  }, [w])
  return { rows, error: rowsError }
}

/** The words that say where work is kept, while the Library does not answer. */
export const KEPT = 'Kept in this browser · Saving to Library when it’s available'

let shelf: boolean | null = null
const shelfListeners = new Set<() => void>()

/**
 * Whether the person's Library answers: asked once, and on the first yes
 * whatever was kept only in this browser is carried up to it.
 */
export function useLibrary(w: Wire): boolean | null {
  const [, bump] = useState(0)
  useEffect(() => {
    const l = () => bump((n) => n + 1)
    shelfListeners.add(l)
    if (shelf === null && w.token())
      void libraryLive(w).then((live) => {
        shelf = live
        shelfListeners.forEach((f) => f())
        if (live) void sync(w).catch(() => undefined)
      })
    return () => {
      shelfListeners.delete(l)
    }
  }, [w])
  return shelf
}

export type { Fleet }
