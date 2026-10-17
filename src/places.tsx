/**
 * The places beside the conversation: the Library (everything this org made),
 * Explore (every starter, and what each needs), and Activity (what is making
 * or rendering now).
 */
import { SizableText, XStack, YStack } from '@hanzo/gui'
import { Download, Search, Trash2 } from '@hanzogui/lucide-icons-2'
import { useEffect, useMemo, useState } from 'react'

import { useHost } from './host.tsx'
import { ALL, kind } from './kinds/index.ts'
import { KEPT, useAvailable, useChats, useLibrary, useWire } from './live.ts'
import { shelved, type KindId, type Shelved } from './made.ts'
import { path } from './route.ts'
import { CATEGORIES, RUNTIME, STARTERS, type Starter } from './starters.ts'
import { Act, download, Pill } from './ui.tsx'
import { useUrl } from './use.ts'
import { ask, list, read, remove, type Wire } from './wire.ts'

/** A starter handed from Explore to the conversation it starts. */
let queued: Starter | null = null
export const queue = (s: Starter) => {
  queued = s
}
export const take = (): Starter | null => {
  const s = queued
  queued = null
  return s
}

function Page({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <YStack flex={1} minH={0} overflow="scroll" bg="var(--pane-fill, var(--sheet-1))" rounded="var(--pane-round, var(--radius-xl))" borderWidth={1} borderColor="var(--pane-edge, var(--white-08))">
      <YStack width="100%" maxW={1160} mx="auto" p="$4" gap="$4">
        <XStack items="center" gap="$3" flexWrap="wrap">
          <SizableText render="h1" size="$7" fontWeight="600" flex={1}>
            {title}
          </SizableText>
          {right}
        </XStack>
        {children}
      </YStack>
    </YStack>
  )
}

/* ----------------------------------------------------------------- library */

interface Item {
  kind: KindId
  chat: string
  made: string
  files: Shelved[]
  modified: number
}

export function Library() {
  const host = useHost()
  const wire = useWire(host)
  const [items, setItems] = useState<Item[] | null>(null)
  const [error, setError] = useState('')
  const [only, setOnly] = useState<KindId | 'all'>('all')
  const [q, setQ] = useState('')
  const shelf = useLibrary(wire)
  const reload = () => {
    setError('')
    list(wire, 'files/').then(
      (entries) => {
        const by = new Map<string, Item>()
        for (const e of entries) {
          const s = shelved(e.key, e.modified, e.size)
          if (!s || s.made === 'attached') continue
          const id = `${s.chat}/${s.made}`
          const it = by.get(id) ?? { kind: s.kind, chat: s.chat, made: s.made, files: [], modified: 0 }
          // The newest version's files stand for the thing.
          const newest = it.files[0]?.version
          if (!newest || s.version > newest) it.files = [s, ...it.files.filter((f) => f.version === s.version)]
          else if (s.version === newest) it.files.push(s)
          it.modified = Math.max(it.modified, s.modified)
          by.set(id, it)
        }
        setItems([...by.values()].sort((a, b) => b.modified - a.modified))
      },
      (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
    )
  }
  useEffect(reload, [wire])
  const kinds = useMemo(() => [...new Set((items ?? []).map((i) => i.kind))], [items])
  const shown = (items ?? []).filter((i) => (only === 'all' || i.kind === only) && (!q || i.files.some((f) => f.name.toLowerCase().includes(q.toLowerCase()))))
  return (
    <Page
      title="Library"
      right={
        <XStack items="center" gap="$2" height={34} px="$3" rounded={999} borderWidth={1} borderColor="$borderColor" minW={220}>
          <Search size={14} />
          <input aria-label="Search the library" placeholder="Search" value={q} onChange={(e) => setQ(e.currentTarget.value)} style={{ flex: 1, border: 0, background: 'transparent', color: 'inherit', font: 'inherit', outline: 'none' }} />
        </XStack>
      }
    >
      <SizableText size="$2" color="$soft">
        {shelf ? 'Saved to your Library, private to you until you share it.' : KEPT}
      </SizableText>
      <XStack gap="$2" flexWrap="wrap" role="toolbar" aria-label="Kinds">
        <Pill on={only === 'all'} onPress={() => setOnly('all')}>
          All
        </Pill>
        {kinds.map((k) => (
          <Pill key={k} on={only === k} onPress={() => setOnly(k)}>
            {kind(k).label}
          </Pill>
        ))}
      </XStack>
      {error ? <SizableText color="$red10">{error}</SizableText> : null}
      {items === null && !error ? <SizableText color="$soft">Reading…</SizableText> : null}
      {items && !shown.length ? <SizableText color="$soft">{items.length ? 'Nothing matches.' : 'Nothing made yet. Start on the home page.'}</SizableText> : null}
      <XStack flexWrap="wrap" gap={12} role="list" aria-label="Made">
        {shown.map((i) => (
          <Thing key={`${i.chat}/${i.made}`} wire={wire} item={i} onOpen={() => host.go(path({ kind: 'chat', id: i.chat }))} onGone={reload} />
        ))}
      </XStack>
    </Page>
  )
}

function Thing({ wire, item, onOpen, onGone }: { wire: Wire; item: Item; onOpen: () => void; onGone: () => void }) {
  const k = kind(item.kind)
  const Icon = k.icon
  const pic = item.files.find((f) => /\.(png|jpe?g|webp)$/i.test(f.name))
  const { src } = useUrl(wire, pic?.key)
  const [busy, setBusy] = useState(false)
  const first = item.files.find((f) => !f.name.endsWith('.json')) ?? item.files[0]!
  const name = first.name
  return (
    <YStack role="listitem" width="calc(50% - 6px)" $sm={{ width: 'calc(25% - 9px)' }} gap="$1.5">
      <YStack render="button" aria-label={`Open ${name} in its conversation`} onPress={onOpen} rounded={16} overflow="hidden" bg="$hover" cursor="pointer" style={{ aspectRatio: '4 / 5' }} items="center" justify="center" position="relative">
        {src ? <img src={src} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} /> : <Icon size={28} />}
        <XStack position="absolute" t={8} l={8} width={26} height={26} rounded={99} bg="rgba(0,0,0,0.55)" items="center" justify="center">
          <Icon size={13} />
        </XStack>
      </YStack>
      <XStack items="center" gap="$1">
        <SizableText size="$2" fontWeight="600" numberOfLines={1} flex={1}>
          {name.replace(/\.[a-z0-9.]+$/i, '').replace(/-/g, ' ')}
        </SizableText>
        <Act label={`Download ${name}`} onPress={() => void read(wire, first.key).then((b) => download(name, b))}>
          <Download size={13} />
        </Act>
        <Act
          label={`Delete ${name}`}
          disabled={busy}
          onPress={() => {
            if (!window.confirm(`Delete ${name} from the library? Its conversation keeps the record.`)) return
            setBusy(true)
            void Promise.all(item.files.map((f) => remove(wire, f.key))).finally(onGone)
          }}
        >
          <Trash2 size={13} />
        </Act>
      </XStack>
      <SizableText size="$1" color="$soft" mt={-4}>
        {`${k.label} · ${new Date(item.modified).toLocaleDateString()}`}
      </SizableText>
    </YStack>
  )
}

/* ----------------------------------------------------------------- explore */

export function Explore() {
  const host = useHost()
  const wire = useWire(host)
  const available = useAvailable(wire)
  return (
    <Page title="Explore">
      {CATEGORIES.filter((c) => c.id !== 'all').map((c) => {
        const list = STARTERS.filter((s) => s.category === c.id)
        if (!list.length) return null
        return (
          <YStack key={c.id} gap="$2">
            <SizableText size="$4" fontWeight="600">
              {c.label}
            </SizableText>
            <XStack flexWrap="wrap" gap="$2">
              {list.map((s) => {
                const why = kind(s.kind).needs(available)
                return (
                  <YStack
                    key={s.id}
                    render="button"
                    aria-label={`${s.title}${why ? `, ${why}` : ''}`}
                    aria-disabled={why ? true : undefined}
                    onPress={() => {
                      if (why) return
                      queue(s)
                      host.go('')
                    }}
                    width={250}
                    p="$3"
                    gap="$1"
                    rounded="$4"
                    borderWidth={1}
                    borderColor="$borderColor"
                    opacity={why ? 0.6 : 1}
                    cursor={why ? 'default' : 'pointer'}
                    hoverStyle={why ? undefined : { bg: '$hover' }}
                  >
                    <XStack justify="space-between">
                      <SizableText size="$3" fontWeight="600" numberOfLines={1}>
                        {s.title}
                      </SizableText>
                      <SizableText size="$1" color="$soft">
                        {RUNTIME[s.kind]}
                      </SizableText>
                    </XStack>
                    <SizableText size="$2" color="$soft" numberOfLines={2}>
                      {why ? 'Needs a GPU: connect one with hanzo link.' : s.subtitle}
                    </SizableText>
                  </YStack>
                )
              })}
            </XStack>
          </YStack>
        )
      })}
      <YStack gap="$2">
        <SizableText size="$4" fontWeight="600">
          Kinds
        </SizableText>
        {ALL.map((k) => (
          <XStack key={k.id} gap="$3" items="center">
            <k.icon size={15} />
            <SizableText size="$2" width={120}>
              {k.label}
            </SizableText>
            <SizableText size="$2" color="$soft" flex={1}>
              {k.needs(available) ?? k.blurb}
            </SizableText>
          </XStack>
        ))}
      </YStack>
    </Page>
  )
}

/* ---------------------------------------------------------------- activity */

interface Job {
  id: string
  kind?: string
  status?: string
  node?: string
  ts?: number
  prompt?: string
}

export function Activity() {
  const host = useHost()
  const wire = useWire(host)
  const { rows } = useChats(wire)
  const available = useAvailable(wire)
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    const read_ = () =>
      ask<{ jobs?: Job[] }>(wire, 'engine', '/v1/render-queue').then(
        (r) => live && (setJobs(r.jobs ?? []), setError('')),
        (e: unknown) => live && setError(e instanceof Error ? e.message : String(e)),
      )
    void read_()
    const t = setInterval(read_, 5000)
    return () => {
      live = false
      clearInterval(t)
    }
  }, [wire])
  const making = rows.filter((r) => r.state === 'making')
  return (
    <Page title="Activity">
      <YStack gap="$2">
        <SizableText size="$4" fontWeight="600">
          GPUs
        </SizableText>
        <SizableText size="$2" color="$soft">
          {available.fleet ? `${available.fleet.online} of ${available.fleet.total} online · ${available.fleet.state}` : available.error || 'Reading…'}
        </SizableText>
        {(available.fleet?.nodes ?? []).map((n) => (
          <SizableText key={n.identity} size="$2">
            {`${n.name ?? n.identity}: ${(n.lanes ?? []).join(', ') || 'no lanes yet'}`}
          </SizableText>
        ))}
      </YStack>
      <YStack gap="$2">
        <SizableText size="$4" fontWeight="600">
          Rendering
        </SizableText>
        {error ? <SizableText color="$red10">{error}</SizableText> : null}
        {jobs && !jobs.length ? <SizableText color="$soft">Nothing is rendering.</SizableText> : null}
        {(jobs ?? []).map((j) => (
          <XStack key={j.id} gap="$3">
            <SizableText size="$2" width={90}>
              {j.status ?? 'queued'}
            </SizableText>
            <SizableText size="$2" flex={1} numberOfLines={1}>
              {j.prompt || j.kind || j.id}
            </SizableText>
            <SizableText size="$2" color="$soft">
              {j.node ?? ''}
            </SizableText>
          </XStack>
        ))}
      </YStack>
      <YStack gap="$2">
        <SizableText size="$4" fontWeight="600">
          Making now
        </SizableText>
        {!making.length ? <SizableText color="$soft">No conversation is working.</SizableText> : null}
        {making.map((r) => (
          <XStack key={r.id} render="button" onPress={() => host.go(path({ kind: 'chat', id: r.id }))} cursor="pointer">
            <SizableText size="$2">{r.title}</SizableText>
          </XStack>
        ))}
      </YStack>
    </Page>
  )
}
