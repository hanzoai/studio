/**
 * One tab: a thing the conversation made. Its bar picks the version shown,
 * compares two, and exports; its body is the kind's own view, which edits in
 * place (a new version, never an overwrite).
 *
 * Compare draws two versions: a wipe for pictures (drag the line), side by
 * side for everything else.
 */
import { SizableText, XStack, YStack } from '@hanzo/gui'
import { ChevronDown, Columns2, Download } from '@hanzogui/lucide-icons-2'
import { DropdownMenu } from '@hanzo/ui'
import { useState } from 'react'

import { kind } from './kinds/index.ts'
import { current, type Made, type Version } from './made.ts'
import { download } from './ui.tsx'
import { useUrl } from './use.ts'
import type { Wire } from './wire.ts'

const ago = (t: number): string => {
  const s = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return new Date(t).toLocaleDateString()
}

export interface DeskProps {
  made: Made
  wire: Wire
  others: readonly Made[]
  /** Show version `at`. */
  onPick: (at: number) => void
  onEdit: (patch: Record<string, unknown>, note: string) => void
  busy: boolean
  /** Why the last edit failed, for this tab. */
  failed?: string
}

export function Desk({ made, wire, others, onPick, onEdit, busy, failed }: DeskProps) {
  const k = kind(made.kind)
  const v = current(made)
  const [against, setAgainst] = useState<number | null>(null)
  const [error, setError] = useState('')
  const exporting = async (i: number) => {
    setError('')
    try {
      const out = await k.exports[i]!.run(wire, made, v)
      download(out.name, out.blob)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  const Icon = k.icon
  const versions = made.versions.map((x, i) => ({
    key: x.id,
    label: `Version ${i + 1}${i === made.at ? ' (shown)' : ''}`,
    description: `${x.note.slice(0, 80)} · ${ago(x.at)}${x.by ? ` · ${x.by}` : ''}`,
    onSelect: () => onPick(i),
  }))
  const compare = against !== null && against !== made.at && made.versions[against] ? made.versions[against]! : null
  return (
    <YStack flex={1} minH={0} data-slot="studio-tab">
      <XStack gap="$1" px="$2" py="$1.5" items="center" borderBottomWidth={1} borderColor="$borderColor" shrink={0} flexWrap="wrap">
        <XStack items="center" gap="$2" flex={1} minW={0} px="$1">
          <Icon size={14} />
          <SizableText size="$2" fontWeight="600" numberOfLines={1}>
            {made.title}
          </SizableText>
        </XStack>
        <DropdownMenu
          trigger={
            <XStack render="button" aria-label="Versions" items="center" gap="$1" height={30} px="$2.5" rounded="$3" cursor="pointer" hoverStyle={{ bg: '$hover' }}>
              <SizableText size="$2">{`v${made.at + 1} of ${made.versions.length}`}</SizableText>
              <ChevronDown size={13} />
            </XStack>
          }
          items={[{ type: 'label', key: 'h', label: 'Versions' }, ...versions]}
        />
        {made.versions.length > 1 ? (
          <DropdownMenu
            trigger={
              <XStack render="button" aria-label="Compare versions" aria-pressed={compare ? true : undefined} items="center" gap="$1.5" height={30} px="$2.5" rounded="$3" bg={compare ? '$hover' : 'transparent'} cursor="pointer" hoverStyle={{ bg: '$hover' }}>
                <Columns2 size={14} />
                <SizableText size="$2">{compare ? `v${(against ?? 0) + 1} | v${made.at + 1}` : 'Compare'}</SizableText>
              </XStack>
            }
            items={[
              { type: 'label', key: 'h', label: 'Compare with' },
              ...made.versions.map((x, i) => ({ key: x.id, label: `Version ${i + 1}`, description: x.note.slice(0, 60), disabled: i === made.at, onSelect: () => setAgainst(i) })),
              ...(compare ? [{ type: 'separator' as const, key: 's' }, { key: 'off', label: 'Stop comparing', onSelect: () => setAgainst(null) }] : []),
            ]}
          />
        ) : null}
        <DropdownMenu
          trigger={
            <XStack render="button" aria-label="Export" items="center" gap="$1.5" height={30} px="$2.5" rounded="$3" cursor="pointer" hoverStyle={{ bg: '$hover' }}>
              <Download size={14} />
              <SizableText size="$2">Export</SizableText>
            </XStack>
          }
          items={[{ type: 'label', key: 'h', label: 'Export' }, ...k.exports.map((x, i) => ({ key: x.label, label: x.label, onSelect: () => void exporting(i) }))]}
        />
      </XStack>
      {error || failed ? (
        <XStack px="$3" py="$2" bg="$hover" shrink={0}>
          <SizableText size="$2" color="$red10">
            {error || failed}
          </SizableText>
        </XStack>
      ) : null}
      {compare ? (
        <Compare made={made} a={compare} b={v} wire={wire} others={others} />
      ) : (
        <k.View made={made} version={v} wire={wire} edit={onEdit} busy={busy} others={others} />
      )}
    </YStack>
  )
}

function Compare({ made, a, b, wire, others }: { made: Made; a: Version; b: Version; wire: Wire; others: readonly Made[] }) {
  const pa = a.files.find((f) => f.mime.startsWith('image/'))
  const pb = b.files.find((f) => f.mime.startsWith('image/'))
  if (pa && pb && (made.kind === 'image' || made.kind === 'poster')) return <Wipe wire={wire} a={pa.key} b={pb.key} title={made.title} />
  const k = kind(made.kind)
  const none = () => undefined
  return (
    <XStack flex={1} minH={0}>
      {[a, b].map((v, i) => (
        <YStack key={v.id} flex={1} minW={0} borderLeftWidth={i ? 1 : 0} borderColor="$borderColor">
          <SizableText size="$1" color="$soft" px="$3" py="$1">
            {`Version ${made.versions.indexOf(v) + 1}`}
          </SizableText>
          <k.View made={made} version={v} wire={wire} edit={none} busy others={others} />
        </YStack>
      ))}
    </XStack>
  )
}

/** Two pictures, one over the other, the line between them dragged across. */
function Wipe({ wire, a, b, title }: { wire: Wire; a: string; b: string; title: string }) {
  const left = useUrl(wire, a)
  const right = useUrl(wire, b)
  const [at, setAt] = useState(50)
  return (
    <YStack flex={1} minH={0} items="center" justify="center" p="$3" gap="$2">
      <div style={{ position: 'relative', maxWidth: '100%', maxHeight: 'calc(100% - 40px)', display: 'inline-block' }}>
        {right.src ? <img src={right.src} alt={`${title}, the version shown`} style={{ display: 'block', maxWidth: '100%', maxHeight: '70vh', borderRadius: 12 }} /> : null}
        {left.src ? (
          <img
            src={left.src}
            alt={`${title}, the version compared`}
            style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain', clipPath: `inset(0 ${100 - at}% 0 0)`, borderRadius: 12 }}
          />
        ) : null}
        <div aria-hidden style={{ position: 'absolute', top: 0, bottom: 0, left: `${at}%`, width: 2, background: '#fff', boxShadow: '0 0 0 1px rgba(0,0,0,.4)' }} />
      </div>
      <input type="range" min={0} max={100} value={at} aria-label="Move the line between the two versions" onChange={(e) => setAt(Number(e.currentTarget.value))} style={{ width: 'min(420px, 100%)' }} />
    </YStack>
  )
}

