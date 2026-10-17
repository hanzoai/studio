/**
 * Studio's addresses, said once. The host hands Studio the path under its
 * mount (`Host.path`, no leading slash) and moves it with `Host.go`; hanzo.ai
 * mounts Studio at /studio and studio.hanzo.ai at its root, so the same
 * grammar reads both.
 *
 *   ''              New: the composer and the starters
 *   c/<id>          one conversation, its tabs beside it
 *   library         everything this org has made
 *   explore         the starters, every kind, with what each needs
 *   activity        what is rendering, queued, done or failed
 *
 * No address here may be a word the engine answers on studio.hanzo.ai, because
 * the edge sends those to it (universe static-sites, the studio route split):
 * /templates, /settings, /models, /history, /view, /upload, /prompt, /login,
 * /callback, /logout, /v1, /ws. That is why the starters are `explore`.
 */

export type Route =
  | { kind: 'new' }
  | { kind: 'chat'; id: string }
  | { kind: 'library' }
  | { kind: 'explore' }
  | { kind: 'activity' }

/** A conversation id: what `fresh` mints, and nothing that could leave the path. */
const ID = /^[a-z0-9][a-z0-9_-]{0,63}$/

export function route(at: string): Route {
  const p = at.replace(/^\/+|\/+$/g, '').split('?')[0]!.split('#')[0]!
  if (!p) return { kind: 'new' }
  const [head, rest, ...more] = p.split('/')
  if (more.length) return { kind: 'new' }
  if (head === 'c' && rest && ID.test(rest)) return { kind: 'chat', id: rest }
  if (head === 'library' && !rest) return { kind: 'library' }
  if (head === 'explore' && !rest) return { kind: 'explore' }
  if (head === 'activity' && !rest) return { kind: 'activity' }
  return { kind: 'new' }
}

export function path(r: Route): string {
  switch (r.kind) {
    case 'new':
      return ''
    case 'chat':
      return `c/${r.id}`
    default:
      return r.kind
  }
}

/** A fresh conversation id: time-ordered, so a listing sorts by name. */
export function fresh(): string {
  const t = Date.now().toString(36).padStart(9, '0')
  const r = Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => (b % 36).toString(36)).join('')
  return `${t}${r}`
}
