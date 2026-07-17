// Layout state for the queue/history rail: divider positions, per-section collapse,
// and the active filter chip — ONE mechanism, persisted in localStorage keyed by
// org+user (from /v1/session) and restored on load. Dividers are native pointer
// drags; nothing here depends on a DnD library.
import { useCallback, useEffect, useRef, useState } from 'react'
import type React from 'react'

export type Filter = 'all' | 'queued' | 'done' | 'favorites' | 'stacks'

export interface Layout {
  railW: number                 // px — the main | rail divider (horizontal resize)
  upFrac: number                // 0..1 — the Up next | History divider (vertical resize)
  collapsed: { next: boolean; up: boolean; hist: boolean }
  filter: Filter
}

export const DEFAULT: Layout = {
  railW: 360, upFrac: 0.5,
  collapsed: { next: false, up: false, hist: false }, filter: 'all',
}
export const RAIL_MIN = 300
export const RAIL_MAX = 640

const KEY = (who: string) => `studio.layout:${who}`
export const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

export function useLayout(who: string) {
  const [layout, setLayout] = useState<Layout>(DEFAULT)
  const loaded = useRef(false)
  const dragging = useRef(false)

  // Restore once we know who the user is (org:user); merge over defaults so a stored
  // layout from an older shape never loses a newly-added field.
  useEffect(() => {
    if (!who || loaded.current) return
    loaded.current = true
    try {
      const raw = localStorage.getItem(KEY(who))
      if (raw) {
        const s = JSON.parse(raw)
        setLayout({ ...DEFAULT, ...s, collapsed: { ...DEFAULT.collapsed, ...(s.collapsed || {}) } })
      }
    } catch { /* corrupt entry — fall back to defaults */ }
  }, [who])

  // Persist on change — but not on every drag frame; the drag commits once on release.
  useEffect(() => {
    if (!who || !loaded.current || dragging.current) return
    try { localStorage.setItem(KEY(who), JSON.stringify(layout)) } catch { /* quota — ignore */ }
  }, [who, layout])

  const patch = useCallback((p: Partial<Layout>) => setLayout(l => ({ ...l, ...p })), [])
  const toggle = useCallback((k: keyof Layout['collapsed']) =>
    setLayout(l => ({ ...l, collapsed: { ...l.collapsed, [k]: !l.collapsed[k] } })), [])
  const reset = useCallback(() => { setLayout(DEFAULT); dragging.current = false }, [])
  const beginDrag = useCallback(() => { dragging.current = true }, [])
  const endDrag = useCallback(() => { dragging.current = false; setLayout(l => ({ ...l })) }, [])

  return { layout, patch, toggle, reset, beginDrag, endDrag }
}

// Native divider drag. Tracks pointer delta from the grab point on the given axis and
// hands it to `onMove` (the caller clamps + applies); `onEnd` commits/persists.
export function startDrag(
  e: React.PointerEvent, axis: 'x' | 'y',
  onMove: (delta: number) => void, onEnd: () => void,
) {
  e.preventDefault()
  const origin = axis === 'x' ? e.clientX : e.clientY
  const move = (ev: PointerEvent) => onMove((axis === 'x' ? ev.clientX : ev.clientY) - origin)
  const up = () => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    document.body.style.userSelect = ''
    document.body.style.cursor = ''
    onEnd()
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
  document.body.style.userSelect = 'none'
  document.body.style.cursor = axis === 'x' ? 'col-resize' : 'row-resize'
}
