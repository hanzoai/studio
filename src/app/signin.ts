/**
 * Signing in on studio.hanzo.ai: one way, through hanzo.ai.
 *
 * Every Hanzo sign-in is the one page at hanzo.ai/login. Studio sends a
 * signed-out person there with a standard OAuth authorize request of its own
 * (the IAM client `hanzo-studio`, PKCE) and where to come back to (`next`);
 * the page signs them in, or answers at once for a browser already signed in,
 * and returns the code to /auth/callback here. The code is exchanged at
 * api.hanzo.ai/v1/iam for the bearer, which is held in this page's memory and
 * nowhere else: a reload goes through hanzo.ai again, which answers without a
 * screen for a person who is signed in there.
 *
 * What the trip needs across the redirect (the verifier, the state, where the
 * person was) is kept in sessionStorage for that one trip and read once.
 */

export const LOGIN = 'https://hanzo.ai/login'
export const CLIENT = 'hanzo-studio'
const TRIP = 'studio.signin'

const b64url = (b: Uint8Array) => btoa(String.fromCharCode(...b)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const random = (n: number) => b64url(crypto.getRandomValues(new Uint8Array(n)))

/** The address of this page's callback. */
export const callback = () => `${window.location.origin}/auth/callback`

/** Leave for hanzo.ai's sign-in, carrying this page's authorize request. */
export async function enter(): Promise<void> {
  const verifier = random(48)
  const state = random(16)
  const challenge = b64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))))
  const back = `${window.location.pathname}${window.location.search}${window.location.hash}`
  try {
    window.sessionStorage.setItem(TRIP, JSON.stringify({ verifier, state, back }))
  } catch {
    // A browser that refuses storage cannot finish the trip; the callback says so.
  }
  const q = new URLSearchParams({
    client_id: CLIENT,
    redirect_uri: callback(),
    response_type: 'code',
    scope: 'openid profile email',
    state,
    code_challenge: challenge,
    code_challenge_method: 'S256',
    next: `${window.location.origin}${back}`,
  })
  window.location.assign(`${LOGIN}?${q}`)
}

export interface Session {
  token: string
  /** Unix ms when it stops working. */
  until: number
}

/** Finish the trip: the code for the bearer. Answers where the person was, or throws why it could not. */
export async function finish(api: string): Promise<{ session: Session; back: string }> {
  const q = new URLSearchParams(window.location.search)
  let trip: { verifier: string; state: string; back: string } | null = null
  try {
    trip = JSON.parse(window.sessionStorage.getItem(TRIP) ?? 'null')
    window.sessionStorage.removeItem(TRIP)
  } catch {
    trip = null
  }
  const err = q.get('error_description') || q.get('error')
  if (err) throw new Error(err)
  const code = q.get('code')
  if (!code || !trip) throw new Error('This sign-in has already been used. Sign in again.')
  if (q.get('state') !== trip.state) throw new Error('This sign-in was not started here. Sign in again.')
  const res = await fetch(`${api}/v1/iam/oauth/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'authorization_code', code, redirect_uri: callback(), client_id: CLIENT, code_verifier: trip.verifier }),
    credentials: 'omit',
  })
  const body = (await res.json().catch(() => ({}))) as { access_token?: string; expires_in?: number; error_description?: string; error?: string; msg?: string }
  if (!res.ok || !body.access_token) throw new Error(body.error_description || body.error || body.msg || `The sign-in answered ${res.status}.`)
  const back = trip.back.startsWith('/') && !trip.back.startsWith('//') && !trip.back.startsWith('/auth/') ? trip.back : '/'
  return { session: { token: body.access_token, until: Date.now() + (body.expires_in ?? 3600) * 1000 }, back }
}

/** What the bearer says about who holds it. */
export interface Claims {
  sub: string
  name: string
  email: string
  picture: string
  orgs: { org: string; role: string }[]
}

export function claims(token: string): Claims | null {
  try {
    const part = token.split('.')[1]!.replace(/-/g, '+').replace(/_/g, '/')
    const c = JSON.parse(decodeURIComponent(escape(atob(part + '='.repeat((4 - (part.length % 4)) % 4))))) as Record<string, unknown>
    const orgs = Array.isArray(c.orgs) ? (c.orgs as { org?: unknown; role?: unknown }[]).filter((o) => typeof o.org === 'string').map((o) => ({ org: String(o.org), role: String(o.role ?? '') })) : []
    return {
      sub: String(c.sub ?? ''),
      name: String(c.displayName ?? c.name ?? ''),
      email: String(c.email ?? ''),
      picture: typeof c.picture === 'string' && c.picture.startsWith('https://') ? c.picture : '',
      orgs,
    }
  } catch {
    return null
  }
}

/** Sign out of hanzo.ai's session as well as this page's, and come back here. */
export function leave(): void {
  const q = new URLSearchParams({ client_id: CLIENT, post_logout_redirect_uri: window.location.origin })
  window.location.assign(`https://hanzo.ai/v1/iam/oauth/logout?${q}`)
}
