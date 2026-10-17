/**
 * Studio: a chat that makes things, every result in a tab beside it.
 *
 * Mount `<Studio host={…} />` under a gui root, in a box with a height. The
 * host is @hanzo/build's `Host`, the same value Dev takes: where the platform
 * is, who is signed in, the org, the address under the mount and how to move
 * it. studio.hanzo.ai mounts it whole, with its own rail. The Hanzo app mounts
 * it with `rail={false}` at /studio and draws Studio's places (`nav`) in its
 * own column: one left column, never two.
 *
 *   ''          New: what should we create?, and the starters
 *   c/<id>      a conversation and its tabs
 *   library     everything made
 *   explore     every starter and kind
 *   activity    what is rendering
 */
import type { Host } from '@hanzo/build'
import { SizableText, XStack, YStack } from '@hanzo/gui'
import { SessionRail, type RailSession } from '@hanzo/ui/chat'
import { useState } from 'react'

import { StudioHost, useHost } from './host.tsx'
import { useChats, useWire } from './live.ts'
import { nav, NEW } from './nav.tsx'
import { Activity, Explore, Library } from './places.tsx'
import { path, route } from './route.ts'
import { Talk } from './talk.tsx'

const DOT = { making: 'running', done: 'done', failed: 'error' } as const

function Pane() {
  const host = useHost()
  const r = route(host.path)
  if (!host.person) {
    return (
      <YStack flex={1} items="center" justify="center" gap="$3" p="$5">
        <SizableText color="$soft">Sign in to create.</SizableText>
        {host.signIn ? (
          <XStack render="button" onPress={() => host.signIn?.()} px="$4" height={36} items="center" rounded={999} bg="$ink" cursor="pointer">
            <SizableText size="$2" color="$background">
              Sign in
            </SizableText>
          </XStack>
        ) : null}
      </YStack>
    )
  }
  if (r.kind === 'library') return <Library />
  if (r.kind === 'explore') return <Explore />
  if (r.kind === 'activity') return <Activity />
  return <Talk id={r.kind === 'chat' ? r.id : null} />
}

/** Studio's own rail, on studio.hanzo.ai: New, the places, the conversations, and the account. */
function Rail() {
  const host = useHost()
  const wire = useWire(host)
  const { rows, error } = useChats(wire)
  const [collapsed, setCollapsed] = useState(false)
  const [drawer, setDrawer] = useState(false)
  const r = route(host.path)
  const go = (p: string) => {
    setDrawer(false)
    host.go(p)
  }
  const links = nav(host, go)
    .flatMap((g) => g.links)
    .filter((l) => l.id !== NEW)
  const recents: RailSession[] = rows.map((row) => ({ id: row.id, title: row.title, status: DOT[row.state] }))
  return (
    <SessionRail
      onNew={() => go('')}
      newLabel="New creation"
      fresh={r.kind === 'new'}
      links={links}
      recents={recents}
      recentsLabel="Recent"
      active={r.kind === 'chat' ? r.id : null}
      onOpen={(id) => go(path({ kind: 'chat', id }))}
      empty={
        <SizableText size="$1" color="$soft" px="$2">
          {error || 'Nothing made yet.'}
        </SizableText>
      }
      account={
        host.person
          ? {
              name: host.person.name || host.person.email,
              sub: host.org ?? undefined,
              email: host.person.email,
              onSignOut: host.signOut,
            }
          : { name: 'Sign in', onPress: () => host.signIn?.() }
      }
      collapsed={collapsed}
      onCollapse={setCollapsed}
      open={drawer}
      onOpenChange={setDrawer}
      label="Studio"
    />
  )
}

/**
 * Studio. `rail={false}` leaves the left column to the host, which lists
 * Studio's places with `nav`; Studio is then a pane of the host's and paints
 * no ground of its own.
 */
export function Studio({ host, rail = true }: { host: Host; rail?: boolean }) {
  return (
    <StudioHost host={host}>
      <XStack flex={1} minH={0} minW={0} height="100%" bg={rail ? '$background' : undefined} gap={rail ? 'var(--pane-gap, var(--space-2))' : 0} p={rail ? 'var(--pane-gap, var(--space-2))' : 0}>
        {rail ? <Rail /> : null}
        <YStack flex={1} minW={0} minH={0}>
          <Pane />
        </YStack>
      </XStack>
    </StudioHost>
  )
}
