import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getWorklog, getQueueStatus, getRatings, getFavorites, getStacks, saveStacks,
  cancelRun, amendRun, uploadRef, rate, setFavorite, saveTemplate, getLineage,
  softDelete, getDeletedPaths, thumbURL, fileURL,
  type Run, type QueueStatus, type QueueItem, type Rating, type Stack, type Lineage,
} from './api'
import { RunCard, runThumb, idLabel, relTime, fmtDur } from './card'
import { type Filter, type Layout, startDrag, clamp } from './layout'

// The queue/history rail — the whole YouTube-watch-page surface adapted to studio's
// generative flows. App owns the shell + composer + layout persistence; this owns all
// queue/history/favorites/stacks data, polling and mutations, and calls back to insert
// a reference into the composer.

// Version chain (pos/len) for each run's output, derived from the loaded rows — the
// same parent→child edges the server's lineage walk uses, counted for the "vN" pips.
function chains(rows: Run[]): Map<string, { pos: number; len: number }> {
  const producer = new Map<string, Run>()
  for (const r of rows) if (r.output && !producer.has(r.output)) producer.set(r.output, r)
  const childrenOf = new Map<string, Run[]>()
  for (const r of rows) for (const par of r.parents || []) {
    const arr = childrenOf.get(par)
    if (arr) arr.push(r); else childrenOf.set(par, [r])
  }
  const out = new Map<string, { pos: number; len: number }>()
  for (const r of rows) {
    if (!r.output) continue
    let anc = 0, cur: string | undefined = r.output
    const seen = new Set([r.output])
    for (;;) {
      const par: string | undefined = producer.get(cur!)?.parents?.[0]
      if (!par || seen.has(par)) break
      seen.add(par); anc++; cur = par
    }
    let desc = 0
    const q = [r.output], seenD = new Set([r.output])
    while (q.length) {
      for (const kid of childrenOf.get(q.shift()!) || []) {
        const key = kid.output || kid.id
        if (seenD.has(key)) continue
        seenD.add(key); desc++
        if (kid.output) q.push(kid.output)
      }
    }
    out.set(r.id, { pos: anc + 1, len: anc + 1 + desc })
  }
  return out
}

export interface RailProps {
  layout: Layout
  patch: (p: Partial<Layout>) => void
  toggle: (k: 'next' | 'up' | 'hist') => void
  reset: () => void
  beginDrag: () => void
  endDrag: () => void
  onUseRef: (text: string) => void
}

export function Rail(props: RailProps) {
  const { layout, patch, toggle, reset, beginDrag, endDrag } = props
  const filter = layout.filter

  const [rows, setRows] = useState<Run[]>([])
  const [q, setQ] = useState<QueueStatus>({ items: {}, medians: {}, lanes: {}, now: Math.floor(Date.now() / 1000) })
  const [now, setNow] = useState(Math.floor(Date.now() / 1000))
  const [ratings, setRatings] = useState<Record<string, Rating>>({})
  const [favorites, setFavorites] = useState<Record<string, { ts: number }>>({})
  const [stacks, setStacks] = useState<Stack[]>([])
  const [deletedPaths, setDeletedPaths] = useState<Set<string>>(new Set())
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [query, setQuery] = useState('')

  const [openStack, setOpenStack] = useState<string | null>(null)
  const [stackPick, setStackPick] = useState<Run | null>(null)
  const [versionsFor, setVersionsFor] = useState<Run | null>(null)
  const [lineage, setLineage] = useState<Lineage | null>(null)
  const [offer, setOffer] = useState<Run | null>(null)
  const [railMenu, setRailMenu] = useState(false)
  const [err, setErr] = useState('')

  const pollLive = useCallback(async () => {
    const [w, qs] = await Promise.all([getWorklog(query), getQueueStatus()])
    setRows(w.items); setQ(qs); setNow(qs.now)
  }, [query])
  const loadRatings = useCallback(() => getRatings().then(setRatings), [])
  const loadFavorites = useCallback(() => getFavorites().then(setFavorites), [])
  const loadStacks = useCallback(() => getStacks().then(setStacks), [])
  const loadDeleted = useCallback(() => getDeletedPaths().then(ps => setDeletedPaths(new Set(ps))), [])

  useEffect(() => { pollLive(); const t = setInterval(pollLive, 4000); return () => clearInterval(t) }, [pollLive])
  useEffect(() => { loadRatings(); loadFavorites(); loadStacks(); loadDeleted() }, [loadRatings, loadFavorites, loadStacks, loadDeleted])
  useEffect(() => { if (!err) return; const t = setTimeout(() => setErr(''), 4000); return () => clearTimeout(t) }, [err])

  // Active = the runs the server still counts as in-flight (present in queue/status),
  // plus just-dispatched rows not yet in the next poll — so a stalled row doesn't
  // linger in Up next forever (matches the queue's own 30-min active window).
  const active = rows.filter(r => (r.status === 'queued' || r.status === 'running') && (q.items[r.id] || now - r.ts <= 1800))
    .sort((a, b) => ((q.items[a.id]?.position ?? 999) - (q.items[b.id]?.position ?? 999)) || (a.ts - b.ts))
  const history = rows.filter(r => (r.status === 'done' || r.status === 'failed' || r.status === 'cancelled')
    && !hidden.has(r.id) && !(r.output && deletedPaths.has(r.output)))
  const favRuns = rows.filter(r => favorites[r.id])
  const nextRun = active[0]
  const chainMap = chains(rows)

  // ── Mutations (each refreshes the affected store) ────────────────────────────────
  // A dispatch/mutation can fail because the run or asset changed since this list was
  // fetched (deduped, finished, cancelled). The trailing refresh already re-fetches;
  // swap the raw error for a plain "pick it again" — one self-heal for every mutation.
  const STALE = /unknown asset|not found|no such job|already finished/i
  const heal = (e: Error): boolean => {
    if (!STALE.test(e.message)) return false
    setErr('That run changed — refreshed, pick it again.')
    return true
  }
  // The ONE remove handler behind both the card's × and its menu Delete: a queued run
  // cancels; a finished result soft-deletes its output (the library lifecycle) and is
  // hidden from History optimistically (reconciled from the server's deleted set).
  async function onRemove(run: Run) {
    if (run.status === 'queued' || run.status === 'running') {
      try { await cancelRun(run.id) } catch (e) { if (!heal(e as Error)) setErr((e as Error).message) }
      pollLive(); return
    }
    setHidden(h => new Set(h).add(run.id))
    const out = run.output
    if (out) {
      try { await softDelete(out); setDeletedPaths(d => new Set(d).add(out)) }
      catch (e) { if (!heal(e as Error)) setErr((e as Error).message) }
    }
    pollLive()
  }
  async function onAmend(run: Run, files: File[]) {
    const names: string[] = []
    for (const f of files) { try { const u = await uploadRef(f); if (u?.name) names.push(u.name) } catch (e) { setErr((e as Error).message) } }
    if (!names.length) return
    const instr = (run.prompt || '').trim()
    try { await amendRun(run.id, instr.length >= 3 ? instr : `${run.kind || 'edit'} update`, names) }
    catch (e) { if (!heal(e as Error)) setErr((e as Error).message) }
    pollLive()
  }
  async function onRate(run: Run, p: { stars?: number; notes?: string; path?: string }) {
    try { await rate(run.id, p) } catch (e) { setErr((e as Error).message) }
    loadRatings()
  }
  async function onFavorite(run: Run) {
    const nowFav = !favorites[run.id]
    try { await setFavorite(run.id, nowFav) } catch (e) { setErr((e as Error).message) }
    await loadFavorites()
    if (nowFav) setOffer(run)                    // offer to turn the flow into a template
  }
  async function onOpenVersions(run: Run) {
    setVersionsFor(run); setLineage(null)
    setLineage(await getLineage(run.output || ''))
  }
  async function persistStacks(next: Stack[]) {
    const clean = next.filter(s => s.items.length >= 2)  // a folder of one dissolves
    setStacks(clean)
    try { setStacks(await saveStacks(clean)) } catch (e) { setErr((e as Error).message) }
  }
  function stackRuns(from: string, target: string) {
    const next = stacks.map(s => ({ ...s, items: s.items.filter(i => i !== from) }))
    const host = next.find(s => s.items.includes(target))
    if (host) host.items = [...host.items, from]
    else next.push({ id: `st_${Date.now().toString(36)}`, name: 'Stack', items: [target, from], ts: Math.floor(Date.now() / 1000) })
    persistStacks(next)
  }
  function addToStack(run: Run, stackId: string) {
    const next = stacks.map(s => ({ ...s, items: s.items.filter(i => i !== run.id) }))
    const host = next.find(s => s.id === stackId)
    if (host) host.items = [...host.items, run.id]
    persistStacks(next); setStackPick(null)
  }
  function unstack(run: Run) {
    persistStacks(stacks.map(s => ({ ...s, items: s.items.filter(i => i !== run.id) })))
  }
  function renameStack(id: string, name: string) {
    setStacks(s => s.map(x => x.id === id ? { ...x, name } : x))
  }
  function commitRename() { persistStacks(stacks) }

  // ── Drag-hold stacking (native HTML5 drag + a 600ms hover timer) ─────────────────
  const dragId = useRef<string | null>(null)
  const holdTimer = useRef<number | undefined>(undefined)
  const [intentId, setIntentId] = useState<string | null>(null)
  function dndFor(run: Run) {
    return {
      onDragStart: (e: React.DragEvent) => { dragId.current = run.id; e.dataTransfer.setData('text/plain', run.id); e.dataTransfer.effectAllowed = 'move' },
      onDragOver: (e: React.DragEvent) => { if (dragId.current && dragId.current !== run.id) { e.preventDefault(); e.dataTransfer.dropEffect = 'move' } },
      onDragEnter: (e: React.DragEvent) => {
        if (!dragId.current || dragId.current === run.id) return
        e.preventDefault()
        window.clearTimeout(holdTimer.current)
        holdTimer.current = window.setTimeout(() => setIntentId(run.id), 600)   // hold to form a stack
      },
      onDragLeave: () => { window.clearTimeout(holdTimer.current); setIntentId(cur => cur === run.id ? null : cur) },
      onDrop: () => {
        window.clearTimeout(holdTimer.current)
        const from = dragId.current, held = intentId === run.id
        setIntentId(null); dragId.current = null
        if (from && from !== run.id && held) stackRuns(from, run.id)
      },
      intent: intentId === run.id,
    }
  }

  const card = (run: Run) => (
    <RunCard
      key={run.id} run={run} qitem={q.items[run.id]} rating={ratings[run.id]}
      favorite={!!favorites[run.id]} now={now} version={chainMap.get(run.id)}
      onRemove={onRemove} onAmend={onAmend} onRate={onRate} onFavorite={onFavorite}
      onAddToStack={r => setStackPick(r)} onOpenVersions={onOpenVersions}
      onUseRef={r => props.onUseRef(idLabel(r))} dnd={dndFor(run)}
    />
  )

  // ── Section layout (content-driven flex + the vertical resize divider) ───────────
  const sectionsRef = useRef<HTMLDivElement>(null)
  const bothOpen = filter === 'all' && !layout.collapsed.up && !layout.collapsed.hist
  const upStyle: React.CSSProperties = filter === 'queued' ? { flex: '1 1 auto' }
    : bothOpen ? { flex: `0 0 ${Math.round(layout.upFrac * 100)}%` }
    : layout.collapsed.up ? { flex: '0 0 auto' } : { flex: '1 1 auto' }
  const histStyle: React.CSSProperties = layout.collapsed.hist ? { flex: '0 0 auto' } : { flex: '1 1 0' }
  function onHSplit(e: React.PointerEvent) {
    beginDrag()
    const h = sectionsRef.current?.getBoundingClientRect().height || 1
    const start = layout.upFrac
    startDrag(e, 'y', d => patch({ upFrac: clamp(start + d / h, 0.15, 0.85) }), endDrag)
  }

  const chips: { k: Filter; label: string; n?: number }[] = [
    { k: 'all', label: 'All' },
    { k: 'queued', label: 'Queued', n: active.length },
    { k: 'done', label: 'Done' },
    { k: 'favorites', label: 'Favorites', n: favRuns.length },
    { k: 'stacks', label: 'Stacks', n: stacks.length },
  ]

  return (
    <>
      {(filter === 'all' || filter === 'queued') && nextRun && (
        <div className={`nextpin${layout.collapsed.next ? ' col' : ''}`}>
          <button className="pin-h" onClick={() => toggle('next')} aria-expanded={!layout.collapsed.next}>
            <span className="pin-lab">Next</span>
            <span className="pin-count">{active.length} in queue</span>
            <span className="chev">{layout.collapsed.next ? '▸' : '▾'}</span>
          </button>
          {!layout.collapsed.next && <NextBody run={nextRun} q={q.items[nextRun.id]} now={now} />}
        </div>
      )}

      <div className="chips">
        <div className="chips-row">
          {chips.map(c => (
            <button key={c.k} className={`chip-f${filter === c.k ? ' on' : ''}`} onClick={() => patch({ filter: c.k })}>
              {c.label}{c.n ? <span className="chip-n">{c.n}</span> : null}
            </button>
          ))}
        </div>
        <div className="rail-more-wrap">
          <button className="rail-more" aria-label="rail options" onClick={() => setRailMenu(m => !m)}>⋯</button>
          {railMenu && (
            <div className="rc-menu" role="menu" onMouseLeave={() => setRailMenu(false)}>
              <button role="menuitem" onClick={() => { setRailMenu(false); reset() }}>Reset layout</button>
              <button role="menuitem" onClick={() => { setRailMenu(false); pollLive(); loadRatings(); loadFavorites(); loadStacks() }}>Refresh</button>
            </div>
          )}
        </div>
      </div>

      <div className="rail-main">
        {filter === 'stacks' ? (
          <div className="stacks-view">
            {stacks.length === 0 && <div className="empty">No stacks yet. Drag one card onto another and hold (~½s) to make a stack.</div>}
            {stacks.map(s => {
              const members = s.items.map(id => rows.find(r => r.id === id)).filter((r): r is Run => !!r)
              const open = openStack === s.id
              return (
                <div key={s.id} className={`stack${open ? ' open' : ''}`}>
                  <button className="stack-tile" onClick={() => setOpenStack(open ? null : s.id)}>
                    <span className="stack-cards">
                      {members.slice(0, 3).map((m, i) => {
                        const src = runThumb(m)
                        return <span key={i} className="sc" style={src ? { backgroundImage: `url(${src})` } : undefined} />
                      })}
                    </span>
                    <span className="stack-meta">
                      <span className="stack-name">{s.name}</span>
                      <span className="stack-count">{members.length}</span>
                    </span>
                  </button>
                  {open && (
                    <div className="stack-open">
                      <input className="stack-rename" value={s.name} aria-label="stack name"
                             onChange={e => renameStack(s.id, e.target.value)} onBlur={commitRename} />
                      {members.map(m => (
                        <div key={m.id} className="stack-member">
                          {card(m)}
                          <button className="unstack" title="remove from stack" onClick={() => unstack(m)}>×</button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ) : filter === 'favorites' ? (
          <section className="sec">
            <div className="sec-body">
              {favRuns.length ? favRuns.map(card) : <div className="empty">No favorites yet. Tap ♥ on any card.</div>}
            </div>
          </section>
        ) : (
          <div className="sections" ref={sectionsRef}>
            {(filter === 'all' || filter === 'queued') && (
              <section className={`sec${layout.collapsed.up ? ' col' : ''}`} style={upStyle}>
                <SecHeader label="Up next" n={active.length} collapsed={layout.collapsed.up} onToggle={() => toggle('up')} />
                {!layout.collapsed.up && (
                  <div className="sec-body">{active.length ? active.map(card) : <div className="empty">Nothing queued. Describe a change to start a run.</div>}</div>
                )}
              </section>
            )}
            {bothOpen && <div className="hsplit" onPointerDown={onHSplit} onDoubleClick={() => patch({ upFrac: 0.5 })} title="drag to resize · double-click to reset" />}
            {(filter === 'all' || filter === 'done') && (
              <section className={`sec${layout.collapsed.hist ? ' col' : ''}`} style={histStyle}>
                <SecHeader label="History" n={history.length} collapsed={layout.collapsed.hist} onToggle={() => toggle('hist')}
                  search={<input className="sec-search" placeholder="Search history…" value={query} onChange={e => setQuery(e.target.value)} />} />
                {!layout.collapsed.hist && (
                  <div className="sec-body">{history.length ? history.map(card) : <div className="empty">No past generations{query ? ' match your search' : ' yet'}.</div>}</div>
                )}
              </section>
            )}
          </div>
        )}
      </div>

      {stackPick && (
        <div className="ovl" onClick={() => setStackPick(null)}>
          <div className="sheet" onClick={e => e.stopPropagation()}>
            <div className="sheet-h">Add “{idLabel(stackPick)}” to a stack</div>
            {stacks.length === 0
              ? <p className="muted">No stacks yet — drag one card onto another and hold to make the first one.</p>
              : stacks.map(s => <button key={s.id} className="sheet-item" onClick={() => addToStack(stackPick, s.id)}>{s.name} · {s.items.length}</button>)}
            <div className="sheet-row"><button onClick={() => setStackPick(null)}>Cancel</button></div>
          </div>
        </div>
      )}

      {versionsFor && (
        <div className="ovl" onClick={() => setVersionsFor(null)}>
          <div className="sheet vers" onClick={e => e.stopPropagation()}>
            <div className="sheet-h">Versions of {idLabel(versionsFor)}</div>
            {!lineage ? <div className="empty">Loading…</div> : (
              <div className="vers-strip">
                {[...lineage.ancestors,
                  { path: lineage.node, prompt: versionsFor.prompt, kind: versionsFor.kind, ts: versionsFor.ts },
                  ...lineage.descendants].map((step, i) => (
                  <a key={i} className={`vstep${step.path === lineage.node ? ' cur' : ''}`}
                     href={step.path ? fileURL(step.path) : undefined} target="_blank" rel="noreferrer"
                     onClick={e => { if (!step.path) e.preventDefault() }} title={step.prompt || step.kind}>
                    {step.path ? <img src={thumbURL(step.path)} alt="" /> : <span className="rc-ph">?</span>}
                    <span className="vlabel">v{i + 1}</span>
                  </a>
                ))}
              </div>
            )}
            <div className="sheet-row"><button onClick={() => setVersionsFor(null)}>Close</button></div>
          </div>
        </div>
      )}

      {offer && <TemplateOffer run={offer} onClose={() => setOffer(null)} onError={setErr} />}
      {err && <div className="rail-toast" role="alert">{err}</div>}
    </>
  )
}

function NextBody({ run, q, now }: { run: Run; q?: QueueItem; now: number }) {
  const src = runThumb(run)
  const badge = run.status === 'running'
    ? (q?.elapsed_seconds != null ? `running · ${fmtDur(q.elapsed_seconds)}` : 'running')
    : (q?.eta_seconds != null ? `starts in ~${fmtDur(q.eta_seconds)}` : 'queued')
  return (
    <div className="pin-body">
      <div className="pin-thumb">{src ? <img src={src} alt="" /> : <span className="rc-ph">{run.kind}</span>}</div>
      <div className="pin-txt">
        <div className="pin-prompt" title={run.prompt}>{run.prompt || `(${run.kind})`}</div>
        <div className="pin-meta">{badge} · {run.node} · {relTime(run.ts, now)}</div>
      </div>
    </div>
  )
}

function SecHeader({ label, n, collapsed, onToggle, search }:
  { label: string; n: number; collapsed: boolean; onToggle: () => void; search?: React.ReactNode }) {
  return (
    <div className="sec-h">
      <button className="sec-toggle" onClick={onToggle} aria-expanded={!collapsed}>
        <span className="chev">{collapsed ? '▸' : '▾'}</span>
        <span className="sec-lab">{label}</span>
        <span className="sec-n">{n}</span>
      </button>
      {search}
    </div>
  )
}

function TemplateOffer({ run, onClose, onError }: { run: Run; onClose: () => void; onError: (m: string) => void }) {
  const [stage, setStage] = useState<'offer' | 'name'>('offer')
  const [name, setName] = useState(idLabel(run))
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  async function save() {
    setBusy(true)
    try {
      await saveTemplate(name.trim() || idLabel(run), { kind: run.kind, prompt: run.prompt, refs: run.refs, parents: run.parents })
      setDone(true); setTimeout(onClose, 900)
    } catch (e) { onError((e as Error).message); onClose() } finally { setBusy(false) }
  }
  return (
    <div className="ovl" onClick={onClose}>
      <div className="sheet" onClick={e => e.stopPropagation()}>
        {done ? <div className="sheet-h">Saved to templates ✓</div>
          : stage === 'offer' ? (
            <>
              <div className="sheet-h">Turn this into a template?</div>
              <p className="muted">Reuse this flow — {run.kind}, its instruction and {run.refs.length} reference{run.refs.length === 1 ? '' : 's'} — as a starting point next time.</p>
              <div className="sheet-row"><button className="pri" onClick={() => setStage('name')}>Name it…</button><button onClick={onClose}>Not now</button></div>
            </>
          ) : (
            <>
              <div className="sheet-h">Name this template</div>
              <input className="sheet-in" value={name} autoFocus onChange={e => setName(e.target.value)} placeholder="Template name" />
              <p className="muted">Captures: {run.kind} · “{run.prompt || '(no instruction)'}” · {run.refs.length} ref{run.refs.length === 1 ? '' : 's'}</p>
              <div className="sheet-row"><button className="pri" disabled={busy || !name.trim()} onClick={save}>{busy ? 'Saving…' : 'Save template'}</button><button onClick={onClose}>Cancel</button></div>
            </>
          )}
      </div>
    </div>
  )
}
