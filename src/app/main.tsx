/**
 * studio.hanzo.ai: Studio mounted whole, with its own rail.
 *
 * The page is its own host: the address is the path, the bearer is held in
 * memory (signin.ts), and a person who is not signed in is sent to hanzo.ai's
 * one sign-in page and comes back. Everything else is the component.
 */
import type { Host } from '@hanzo/build'
import { Hanzo, Toaster, YStack } from '@hanzo/ui'
import '@hanzo/font/css'
import '@hanzo/ui/theme.css'
import '@hanzo/ui/styles/motion.css'
import { SizableText } from '@hanzo/gui'
import { ThemeProvider, useTheme } from 'next-themes'
import { StrictMode, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'

import { Studio } from '../studio.tsx'
import { claims, enter, finish, leave, type Session } from './signin.ts'

;(globalThis as { __HANZO_TELEMETRY__?: { enabled?: boolean } }).__HANZO_TELEMETRY__ = { enabled: false }

/** The platform: same origin on a laptop (vite proxies it), api.hanzo.ai otherwise. */
const API = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1' ? '' : 'https://api.hanzo.ai'

let session: Session | null = null
const ORG = 'studio.org'

/* The address, as one store: the path, moved with the History API. */
const moves = new Set<() => void>()
window.addEventListener('popstate', () => moves.forEach((l) => l()))
const here = () => window.location.pathname
function useAddress(): string {
  return useSyncExternalStore(
    (l) => {
      moves.add(l)
      return () => moves.delete(l)
    },
    here,
    here,
  )
}
function go(to: string, replace = false) {
  const p = `/${to.replace(/^\/+/, '')}`
  if (p === here()) return
  if (replace) window.history.replaceState(null, '', p)
  else window.history.pushState(null, '', p)
  moves.forEach((l) => l())
}

/** The code is spent once: a second effect run (React's strict mode) reads the same answer. */
let finishing: ReturnType<typeof finish> | null = null
let leaving = false

function Callback({ onSignedIn }: { onSignedIn: (s: Session, back: string) => void }) {
  const [error, setError] = useState('')
  useEffect(() => {
    finishing ??= finish(API)
    finishing.then(
      ({ session: s, back }) => onSignedIn(s, back),
      (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
    )
  }, [onSignedIn])
  return (
    <YStack flex={1} items="center" justify="center" gap="$3">
      <SizableText color="$soft">{error || 'Signing you in…'}</SizableText>
      {error ? (
        <SizableText size="$2" color="$ink" cursor="pointer" onPress={() => void enter()} role="link">
          Sign in again
        </SizableText>
      ) : null}
    </YStack>
  )
}

function App() {
  const at = useAddress()
  const [signed, setSigned] = useState<Session | null>(session)
  const [org, setOrg] = useState<string | null>(() => {
    try {
      return window.localStorage.getItem(ORG)
    } catch {
      return null
    }
  })

  // Signed out on any address but the callback: to hanzo.ai and back.
  useEffect(() => {
    if (!signed && at !== '/auth/callback' && !leaving) {
      leaving = true
      void enter()
    }
  }, [signed, at])
  // A bearer that has run out is a sign-in, before anything is refused with it.
  useEffect(() => {
    if (!signed) return
    const t = setTimeout(() => {
      session = null
      setSigned(null)
    }, Math.max(0, signed.until - Date.now() - 30_000))
    return () => clearTimeout(t)
  }, [signed])

  const who = useMemo(() => (signed ? claims(signed.token) : null), [signed])
  const memberships = useMemo(() => (who?.orgs ?? []).map((o) => o.org), [who])
  const scoped = org && memberships.includes(org) ? org : (memberships[0] ?? null)

  const host = useMemo<Host>(
    () => ({
      name: 'Hanzo Studio',
      api: API,
      token: () => session?.token ?? null,
      org: scoped,
      memberships,
      chooseOrg: (next) => {
        if (!memberships.includes(next)) return
        try {
          window.localStorage.setItem(ORG, next)
        } catch {
          // Kept for this page.
        }
        setOrg(next)
        go('')
      },
      person: who ? { name: who.name, email: who.email, avatar: who.picture } : null,
      admin: Boolean(who?.orgs.some((o) => o.org === scoped && (o.role === 'admin' || o.role === 'owner'))),
      path: at.replace(/^\/+/, ''),
      go: (to, how) => go(to, how?.replace),
      links: { github: 'https://hanzo.ai/dev/projects', customize: 'https://hanzo.ai/dev/customize', settings: 'https://hanzo.ai/settings', home: '/' },
      open: (href) => window.location.assign(href),
      signIn: () => void enter(),
      signOut: () => {
        session = null
        leave()
      },
    }),
    [scoped, memberships, who, at],
  )

  if (at === '/auth/callback') {
    return (
      <Callback
        onSignedIn={(s, back) => {
          session = s
          setSigned(s)
          go(back, true)
        }}
      />
    )
  }
  if (!signed) {
    return (
      <YStack flex={1} items="center" justify="center">
        <SizableText color="$soft">Signing you in…</SizableText>
      </YStack>
    )
  }
  return <Studio host={host} />
}

function Themed({ children }: { children: ReactNode }) {
  const { resolvedTheme } = useTheme()
  return <Hanzo theme={resolvedTheme === 'light' ? 'light' : 'dark'}>{children}</Hanzo>
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider attribute="class" defaultTheme="dark" enableSystem>
      <Themed>
        <YStack flex={1} minW={0} bg="$background" style={{ height: '100dvh', overflow: 'hidden' }}>
          <App />
        </YStack>
        <Toaster />
      </Themed>
    </ThemeProvider>
  </StrictMode>,
)
