import { useState } from 'react'
import {
  type Run, type QueueItem, type Rating,
  thumbURL, inputThumbURL, fileURL,
} from './api'

// ── Small shared bits ────────────────────────────────────────────────────────────
export function relTime(ts: number, now: number): string {
  const s = Math.max(0, now - ts)
  if (s < 45) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}
export function fmtDur(sec: number): string {
  if (sec < 60) return `${Math.round(sec)}s`
  if (sec < 3600) return `${Math.round(sec / 60)}m`
  return `${(sec / 3600).toFixed(1)}h`
}
// A friendly identifier for a run, from its output/prefix (the finding: cards must
// carry names like the "design 13" the prompts teach). Strips the _NNNNN_ counter.
export function idLabel(run: Run): string {
  const raw = (run.output || run.output_prefix || run.kind || '').split('/').pop() || run.kind
  return raw.replace(/_\d+_?\.\w+$/, '').replace(/_\d+_$/, '').replace(/\.\w+$/, '') || run.kind
}
export function runThumb(run: Run): string | null {
  if (run.output) return thumbURL(run.output)
  if (run.parents[0]) return thumbURL(run.parents[0])
  if (run.refs[0]) return inputThumbURL(run.refs[0])
  return null
}
const isActive = (run: Run) => run.status === 'queued' || run.status === 'running'

export function Stars({ value, onChange }: { value: number; onChange?: (n: number) => void }) {
  const stars = [1, 2, 3, 4, 5]
  return (
    <span className={`stars${onChange ? ' interactive' : ''}`} role={onChange ? 'radiogroup' : undefined} aria-label="rating">
      {stars.map(n => (
        <button
          key={n} type="button" className={`star${n <= value ? ' on' : ''}`}
          disabled={!onChange} aria-label={`${n} star${n > 1 ? 's' : ''}`}
          onClick={onChange ? () => onChange(n === value ? 0 : n) : undefined}
        >★</button>
      ))}
    </span>
  )
}

// ── One run card ─────────────────────────────────────────────────────────────────
export interface CardProps {
  run: Run
  qitem?: QueueItem
  rating?: Rating
  favorite: boolean
  now: number
  version?: { pos: number; len: number }
  onRemove: (run: Run) => void
  onAmend: (run: Run, files: File[]) => Promise<void>
  onRate: (run: Run, patch: { stars?: number; notes?: string; path?: string }) => void
  onFavorite: (run: Run) => void
  onAddToStack: (run: Run) => void
  onOpenVersions: (run: Run) => void
  onUseRef: (run: Run) => void
  dnd: {
    onDragStart: (e: React.DragEvent) => void
    onDragEnter: (e: React.DragEvent) => void
    onDragOver: (e: React.DragEvent) => void
    onDragLeave: (e: React.DragEvent) => void
    onDrop: (e: React.DragEvent) => void
    intent: boolean
  }
}

export function RunCard(p: CardProps) {
  const { run, qitem, rating, favorite, now, version } = p
  const [menu, setMenu] = useState(false)
  const [panel, setPanel] = useState<'none' | 'rate' | 'refs'>('none')
  const [stars, setStars] = useState(rating?.stars ?? 0)
  const [notes, setNotes] = useState(rating?.notes ?? '')
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [fileOver, setFileOver] = useState(false)
  const [confirmDel, setConfirmDel] = useState(false)

  const active = isActive(run)
  const src = runThumb(run)
  const fresh = run.status === 'done' && now - (run.finished_at || run.ts) < 120

  // Corner badge: ETA while queued, elapsed while running, stars-or-node when done.
  let badge = ''
  if (run.status === 'running') badge = qitem?.elapsed_seconds != null ? fmtDur(qitem.elapsed_seconds) : 'running'
  else if (run.status === 'queued') badge = qitem?.eta_seconds != null ? `~${fmtDur(qitem.eta_seconds)}` : 'queued'
  else if (run.status === 'failed') badge = 'failed'
  else if (run.status === 'cancelled') badge = 'cancelled'
  else badge = rating?.stars ? `★${rating.stars}` : run.node

  const pos = qitem?.position && qitem?.of ? `${qitem.position}/${qitem.of}` : ''
  const meta = [run.node, relTime(run.ts, now), run.status + (pos ? ` · ${pos}` : '')].filter(Boolean).join(' · ')

  const collectFiles = (list: FileList | null) =>
    setFiles(f => [...f, ...[...(list || [])].filter(x => x.type.startsWith('image/'))])

  function onDrop(e: React.DragEvent) {
    const dropped = [...(e.dataTransfer?.files || [])].filter(x => x.type.startsWith('image/'))
    if (dropped.length && active) {           // files onto a queued card → add references
      e.preventDefault(); e.stopPropagation(); setFileOver(false)
      setPanel('refs'); setFiles(f => [...f, ...dropped]); return
    }
    setFileOver(false); p.dnd.onDrop(e)         // otherwise an internal run drag → stacking
  }
  function onDragOver(e: React.DragEvent) {
    if (active && e.dataTransfer?.types?.includes('Files')) { e.preventDefault(); setFileOver(true); return }
    p.dnd.onDragOver(e)                          // internal run drag → let the stack drop fire
  }

  async function applyRefs() {
    if (!files.length || busy) return
    setBusy(true)
    try { await p.onAmend(run, files); setFiles([]); setPanel('none') } finally { setBusy(false) }
  }
  function saveRate() {
    p.onRate(run, { stars, notes, path: run.output || undefined })
    setPanel('none')
  }
  // The × (and the menu's Delete) share ONE handler: a queued run cancels straight
  // away; a finished result is a destructive soft-delete, so it asks first.
  function requestDelete() {
    if (active) p.onRemove(run)
    else setConfirmDel(true)
  }

  return (
    <article
      className={`rc rc-${run.status}${p.dnd.intent ? ' stack-intent' : ''}${fileOver ? ' file-over' : ''}`}
      draggable onDragStart={p.dnd.onDragStart}
      onDragEnter={p.dnd.onDragEnter} onDragLeave={e => { setFileOver(false); p.dnd.onDragLeave(e) }}
      onDragOver={onDragOver} onDrop={onDrop}
    >
      <a className="rc-thumb" draggable={false} href={run.output ? fileURL(run.output) : undefined}
         target="_blank" rel="noreferrer" onClick={e => { if (!run.output) e.preventDefault() }}>
        {src ? <img loading="lazy" draggable={false} src={src} alt="" /> : <span className="rc-ph">{run.kind || '—'}</span>}
        <span className={`rc-badge s-${run.status}`}>{badge}</span>
        {fresh && <span className="rc-new">New</span>}
      </a>

      <button className="rc-x" aria-label={active ? 'cancel' : 'delete'}
              title={active ? 'Cancel' : 'Delete'} onClick={requestDelete}>×</button>
      {confirmDel && (
        <div className="rc-confirm" role="alertdialog" aria-label="confirm delete">
          <span>Delete result?</span>
          <button className="danger" onClick={() => { setConfirmDel(false); p.onRemove(run) }}>Delete</button>
          <button onClick={() => setConfirmDel(false)}>Keep</button>
        </div>
      )}

      <div className="rc-body">
        <div className="rc-title" title={run.prompt || run.kind}>{run.prompt || `(${run.kind})`}</div>
        <div className="rc-meta">{meta}</div>
        <div className="rc-tags">
          <span className="rc-id">{idLabel(run)}</span>
          {version && version.len > 1 && (
            <button className="rc-vers" title="versions of this design" onClick={() => p.onOpenVersions(run)}>
              {Array.from({ length: Math.min(version.len, 6) }).map((_, i) =>
                <span key={i} className={`pip${i === version.pos - 1 ? ' on' : ''}`} />)}
              <span className="rc-vn">v{version.pos}</span>
            </button>
          )}
          {favorite && <span className="rc-fav" title="favorite">♥</span>}
          {rating?.stars ? <span className="rc-rated"><Stars value={rating.stars} /></span> : null}
        </div>
      </div>

      <button className="rc-more" aria-label="more actions" aria-haspopup="menu"
              onClick={() => setMenu(m => !m)}>⋯</button>

      {menu && (
        <div className="rc-menu" role="menu" onMouseLeave={() => setMenu(false)}>
          <button role="menuitem" onClick={() => { setMenu(false); requestDelete() }}>Delete</button>
          {active && <button role="menuitem" onClick={() => { setMenu(false); setPanel('refs') }}>Add references</button>}
          <button role="menuitem" onClick={() => { setMenu(false); setStars(rating?.stars ?? 0); setNotes(rating?.notes ?? ''); setPanel('rate') }}>Rate &amp; note</button>
          <button role="menuitem" onClick={() => { setMenu(false); p.onFavorite(run) }}>{favorite ? 'Unfavorite' : 'Favorite'}</button>
          <button role="menuitem" onClick={() => { setMenu(false); p.onAddToStack(run) }}>Add to stack</button>
          <button role="menuitem" onClick={() => { setMenu(false); p.onUseRef(run) }}>Use as reference</button>
        </div>
      )}

      {panel === 'rate' && (
        <div className="rc-panel">
          <Stars value={stars} onChange={setStars} />
          <textarea className="rc-notes" value={notes} onChange={e => setNotes(e.target.value)}
                    placeholder="What should the AI do differently next time?" />
          <div className="rc-panel-row">
            <button className="pri" onClick={saveRate}>Save</button>
            <button onClick={() => setPanel('none')}>Cancel</button>
          </div>
        </div>
      )}

      {panel === 'refs' && (
        <div className={`rc-panel rc-drop${fileOver ? ' over' : ''}`}
             onDragOver={e => { e.preventDefault(); setFileOver(true) }}
             onDragLeave={() => setFileOver(false)}
             onDrop={e => { e.preventDefault(); e.stopPropagation(); setFileOver(false); collectFiles(e.dataTransfer.files) }}>
          <div className="rc-drop-hint">Drop images here, or</div>
          <label className="rc-pick">Choose files
            <input type="file" multiple accept="image/*" onChange={e => collectFiles(e.target.files)} />
          </label>
          {files.length > 0 && <div className="rc-files">{files.map((f, i) => <span key={i} className="chip">{f.name}</span>)}</div>}
          <div className="rc-panel-row">
            <button className="pri" disabled={!files.length || busy} onClick={applyRefs}>
              {busy ? 'Requeuing…' : `Add ${files.length || ''} & requeue`}
            </button>
            <button onClick={() => { setFiles([]); setPanel('none') }}>Cancel</button>
          </div>
        </div>
      )}
    </article>
  )
}
