/**
 * Under the composer while a conversation is empty: the category pills, the
 * starters that can run right now, and the last conversations. A starter
 * starts a conversation with its first message; one that starts from a
 * picture puts its words in the composer and asks for the picture.
 */
import { SizableText, XStack, YStack } from '@hanzo/gui'
import { ArrowRight } from '@hanzogui/lucide-icons-2'
import { useState } from 'react'

import { useHost } from './host.tsx'
import { kind } from './kinds/index.ts'
import type { Available } from './kinds/kind.ts'
import { stack, use } from './kinds/type/fonts.ts'
import { PRESETS } from './kinds/type/system.ts'
import { useChats, useWire } from './live.ts'
import { path } from './route.ts'
import { CATEGORIES, RUNTIME, STARTERS, type Category, type Starter } from './starters.ts'
import { Pill } from './ui.tsx'
import { useUrl } from './use.ts'
import type { Wire } from './wire.ts'

export function Home({ available, onStart }: { available: Available; onStart: (s: Starter) => void }) {
  const [cat, setCat] = useState<Category | 'all'>('all')
  const runs = STARTERS.filter((s) => !kind(s.kind).needs(available))
  const shown = runs.filter((s) => cat === 'all' || s.category === cat)
  const cats = CATEGORIES.filter((c) => c.id === 'all' || runs.some((s) => s.category === c.id))
  return (
    <YStack data-slot="studio-home" width="100%" maxW={1080} mx="auto" gap="$4" pt="$4" pb="$6" px="$1">
      <XStack role="toolbar" aria-label="Categories" gap="$2" flexWrap="wrap" justify="center">
        {cats.map((c) => (
          <Pill key={c.id} on={cat === c.id} onPress={() => setCat(c.id)}>
            {c.label}
          </Pill>
        ))}
      </XStack>
      <XStack role="list" aria-label="Starters" flexWrap="wrap" gap={12}>
        {shown.map((s, i) => (
          <Card key={s.id} s={s} i={i} onPress={() => onStart(s)} />
        ))}
      </XStack>
      <Recent />
    </YStack>
  )
}

/** A starter's card: 4:5, its picture, or its title set in type; its words under it. */
function Card({ s, i, onPress }: { s: Starter; i: number; onPress: () => void }) {
  const k = kind(s.kind)
  const Icon = k.icon
  const look = PRESETS[i % PRESETS.length]!
  use(look.display)
  return (
    <YStack role="listitem" width="calc(50% - 6px)" $sm={{ width: 'calc(25% - 9px)' }} gap="$1.5">
      <YStack position="relative" width="100%" rounded={16} overflow="hidden" style={{ aspectRatio: '4 / 5' }}>
        {s.thumb ? (
          <img src={s.thumb} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
        ) : (
          <div aria-hidden style={{ position: 'absolute', inset: 0, background: look.colors.paper, color: look.colors.ink, padding: '14% 10%', display: 'flex', flexDirection: 'column', justifyContent: 'flex-end', textAlign: 'left' }}>
            <div style={{ width: '28%', height: 4, background: look.colors.accent, marginBottom: 10 }} />
            <div style={{ fontFamily: stack(look.display), fontWeight: look.weights.display, fontSize: 'clamp(18px, 2.4vw, 30px)', lineHeight: look.leading.display, letterSpacing: `${look.tracking.display}em` }}>{s.title}</div>
          </div>
        )}
        <XStack aria-hidden position="absolute" t={8} l={8} r={8} justify="space-between" items="center">
          <XStack width={26} height={26} rounded={99} bg="rgba(0,0,0,0.55)" items="center" justify="center">
            <Icon size={13} />
          </XStack>
          <XStack px="$2" height={22} rounded={99} bg="rgba(0,0,0,0.55)" items="center">
            <SizableText size="$1" color="#fff">
              {RUNTIME[s.kind]}
            </SizableText>
          </XStack>
        </XStack>
        {/* The card's one control, over the preview and holding none of it, so its name is its label alone. */}
        <YStack
          render="button"
          aria-label={`${s.title}: ${s.subtitle}`}
          onPress={onPress}
          position="absolute"
          t={0}
          l={0}
          r={0}
          b={0}
          rounded={16}
          cursor="pointer"
          hoverStyle={{ bg: 'rgba(255,255,255,0.08)' }}
          focusVisibleStyle={{ outlineWidth: 2, outlineStyle: 'solid', outlineColor: '$outlineColor' }}
        />
      </YStack>
      <SizableText size="$2" fontWeight="600" numberOfLines={1}>
        {s.title}
      </SizableText>
      <SizableText size="$1" color="$soft" numberOfLines={1} mt={-4}>
        {s.subtitle}
      </SizableText>
    </YStack>
  )
}

const STATE = { making: 'Making', done: 'Done', failed: 'Failed' } as const

/** The last eight conversations, each with what it is doing now. */
function Recent() {
  const host = useHost()
  const wire = useWire(host)
  const { rows } = useChats(wire)
  if (!rows.length) return null
  return (
    <YStack gap="$2" pt="$2">
      <XStack items="center" justify="space-between">
        <SizableText size="$3" fontWeight="600">
          Recent
        </SizableText>
        <XStack render="button" aria-label="Open the library" onPress={() => host.go(path({ kind: 'library' }))} items="center" gap="$1" cursor="pointer" px="$2" height={28} rounded="$3" hoverStyle={{ bg: '$hover' }}>
          <SizableText size="$2" color="$soft">
            Library
          </SizableText>
          <ArrowRight size={13} />
        </XStack>
      </XStack>
      <XStack role="list" aria-label="Recent conversations" gap="$2" overflowX="auto" pb="$1">
        {rows.slice(0, 8).map((r) => (
          <YStack
            role="listitem"
            key={r.id}
            render="button"
            aria-label={`${r.title}, ${STATE[r.state]}`}
            onPress={() => host.go(path({ kind: 'chat', id: r.id }))}
            width={148}
            shrink={0}
            gap="$1"
            cursor="pointer"
          >
            <Cover wire={wire} cover={r.cover} />
            <SizableText size="$2" numberOfLines={1}>
              {r.title}
            </SizableText>
            <XStack items="center" gap="$1.5">
              <XStack width={6} height={6} rounded={6} bg={r.state === 'making' ? '$blue10' : r.state === 'failed' ? '$red10' : '$green10'} />
              <SizableText size="$1" color="$soft">
                {`${STATE[r.state]} · ${r.count} made`}
              </SizableText>
            </XStack>
          </YStack>
        ))}
      </XStack>
    </YStack>
  )
}

function Cover({ wire, cover }: { wire: Wire; cover?: string }) {
  const { src } = useUrl(wire, cover)
  return src ? (
    <img src={src} alt="" width={148} height={110} style={{ borderRadius: 12, objectFit: 'cover', width: 148, height: 110 }} />
  ) : (
    <YStack width={148} height={110} rounded={12} bg="$hover" />
  )
}
