/**
 * Small hooks every tab uses: a stored file's address, and its text.
 */
import { useEffect, useState } from 'react'

import { read, url, type Wire } from './wire.ts'

/** A stored file's read address, minted when drawn; '' until it is, and on failure. */
export function useUrl(w: Wire, key: string | undefined): { src: string; error: string } {
  const [state, setState] = useState({ src: '', error: '' })
  useEffect(() => {
    if (!key) return
    let live = true
    url(w, key).then(
      (src) => live && setState({ src, error: '' }),
      (e: unknown) => live && setState({ src: '', error: e instanceof Error ? e.message : String(e) }),
    )
    return () => {
      live = false
    }
  }, [w, key])
  return state
}

/** A stored file's text, read when drawn. */
export function useText(w: Wire, key: string | undefined): { text: string | null; error: string } {
  const [state, setState] = useState<{ text: string | null; error: string }>({ text: null, error: '' })
  useEffect(() => {
    if (!key) return
    let live = true
    read(w, key)
      .then((b) => b.text())
      .then(
        (text) => live && setState({ text, error: '' }),
        (e: unknown) => live && setState({ text: null, error: e instanceof Error ? e.message : String(e) }),
      )
    return () => {
      live = false
    }
  }, [w, key])
  return state
}
