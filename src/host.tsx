/**
 * The host Studio is mounted under, for every pane: @hanzo/build's `Host`, the
 * same value Dev takes, so hanzo.ai mounts both one way. It is also handed to
 * @hanzo/build's own provider, which its side panel reads.
 */
import { HostProvider, type Host } from '@hanzo/build'
import { createContext, useContext, type ReactNode } from 'react'

const Ctx = createContext<Host | null>(null)

export function StudioHost({ host, children }: { host: Host; children: ReactNode }) {
  return (
    <HostProvider host={host}>
      <Ctx.Provider value={host}>{children}</Ctx.Provider>
    </HostProvider>
  )
}

export function useHost(): Host {
  const h = useContext(Ctx)
  if (!h) throw new Error('Studio was drawn without a host')
  return h
}
