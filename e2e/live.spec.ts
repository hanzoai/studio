/**
 * Studio, live, as a signed-in person: sign-in through IAM with the hanzo-studio
 * client (PKCE), the home's starters, things made end to end (each a tab, then in
 * Recent and the Library, which a fresh browser reads back), what one person keeps
 * hidden from another in the org and from another org, a phone's width, and axe.
 *
 * The sign-in page itself (hanzo.ai/login) is another surface's; this signs the
 * account in with the same request that page answers (POST /v1/iam/login for the
 * code Studio asked for) and returns the code to Studio's own callback, so the
 * PKCE exchange, the callback and the session are Studio's real ones.
 */
import { createHash, randomBytes } from 'node:crypto'

import AxeBuilder from '@axe-core/playwright'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const USER = process.env.IAM_USER ?? ''
const PASS = process.env.IAM_PASS ?? ''
const ORG = process.env.IAM_ORG ?? ''
/** A second account: a member of IAM_ORG, with an org of its own. */
const OTHER = { user: process.env.IAM_OTHER_USER ?? '', pass: process.env.IAM_OTHER_PASS ?? '', org: process.env.IAM_OTHER_ORG ?? '' }
const API = 'https://api.hanzo.ai'

test.skip(!USER || !PASS || !ORG, 'IAM_ORG, IAM_USER and IAM_PASS name the account the gate signs in as')

async function signIn(page: Page): Promise<void> {
  const base = new URL(test.info().project.use.baseURL!)
  if (process.env.COOKIE) await page.context().addCookies([{ name: process.env.COOKIE.split('=')[0]!, value: process.env.COOKIE.split('=')[1]!, domain: base.hostname, path: '/', secure: true }])
  const sent = page.waitForURL(/hanzo\.ai\/login\?/)
  await page.goto('/')
  await sent
  const asked = new URL(page.url()).searchParams
  expect(asked.get('client_id')).toBe('hanzo-studio')
  expect(asked.get('code_challenge_method')).toBe('S256')
  const r = await page.request.post(`${API}/v1/iam/login`, {
    data: {
      type: 'code',
      clientId: asked.get('client_id'),
      application: asked.get('client_id'),
      organization: ORG,
      username: USER,
      password: PASS,
      redirectUri: asked.get('redirect_uri'),
      scope: asked.get('scope'),
      state: asked.get('state'),
      codeChallenge: asked.get('code_challenge'),
      codeChallengeMethod: 'S256',
    },
  })
  const body = (await r.json()) as { status?: string; msg?: string; data?: string }
  expect(body.status, body.msg).not.toBe('error')
  await page.goto(`${asked.get('redirect_uri')}?${new URLSearchParams({ code: body.data!, state: asked.get('state')! })}`)
  await expect(page.getByRole('heading', { name: 'What should we create?' })).toBeVisible()
}

/** A bearer for an account, minted the way Studio's page gets one: a PKCE code, then the exchange. */
async function bearer(request: APIRequestContext, user: string, pass: string, org: string): Promise<string> {
  const verifier = randomBytes(48).toString('base64url')
  const redirectUri = 'https://studio.hanzo.ai/auth/callback'
  const r = await request.post(`${API}/v1/iam/login`, {
    data: {
      type: 'code',
      clientId: 'hanzo-studio',
      application: 'hanzo-studio',
      organization: org,
      username: user,
      password: pass,
      redirectUri,
      scope: 'openid profile email',
      state: 's',
      codeChallenge: createHash('sha256').update(verifier).digest('base64url'),
      codeChallengeMethod: 'S256',
    },
  })
  const code = ((await r.json()) as { data?: string }).data
  expect(code, 'IAM issued a code').toBeTruthy()
  const t = await request.post(`${API}/v1/iam/oauth/token`, { form: { grant_type: 'authorization_code', code: code!, client_id: 'hanzo-studio', redirect_uri: redirectUri, code_verifier: verifier } })
  const token = ((await t.json()) as { access_token?: string }).access_token
  expect(token, 'IAM exchanged the code').toBeTruthy()
  return token!
}

/** The Studio items a bearer's Library lists in an org. */
async function shelf(request: APIRequestContext, token: string, org: string): Promise<{ status: number; ids: string[] }> {
  const r = await request.get(`${API}/v1/library?source=studio&limit=200`, { headers: { Authorization: `Bearer ${token}`, 'X-Org-Id': org } })
  const ids = r.ok() ? (((await r.json()) as { data?: { id: string }[] }).data ?? []).map((i) => i.id) : []
  return { status: r.status(), ids }
}

/** Every picture on the page loads from this host, a blob, inline data or one of ours. */
async function ours(page: Page): Promise<string[]> {
  return page.$$eval('img', (imgs) =>
    imgs
      .map((i) => (i as HTMLImageElement).currentSrc || (i as HTMLImageElement).src)
      .filter((s) => s && !s.startsWith('blob:') && !s.startsWith('data:') && !/^https:\/\/([a-z0-9-]+\.)*hanzo\.(ai|app)\//.test(s) && !s.startsWith(location.origin)),
  )
}

/*
 * Navigation stays inside the app: the bearer is held in memory, so loading a
 * page again signs the person out to hanzo.ai, as it should.
 */
async function home(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'New creation' }).first().click()
  await expect(page.getByRole('heading', { name: 'What should we create?' })).toBeVisible()
}

/** Start from a starter and wait for its tab. */
async function make(page: Page, category: string, card: RegExp, tab: RegExp): Promise<void> {
  await home(page)
  await page.getByRole('button', { name: category, exact: true }).click()
  await page.getByRole('button', { name: card }).click()
  await expect(page.getByRole('tab', { name: tab })).toBeVisible({ timeout: 4 * 60_000 })
}

test('signed in, the home offers at least twelve starters from our hosts only', async ({ page }) => {
  await signIn(page)
  const cards = page.getByRole('list', { name: 'Starters' }).getByRole('listitem')
  expect(await cards.count()).toBeGreaterThanOrEqual(12)
  expect(await ours(page)).toEqual([])
})

test('makes a type system, a poster, a document, slides and a voice, each a tab, then in Recent and the Library', async ({ page, browser }) => {
  await signIn(page)
  await make(page, 'Type', /Coffee roaster brand/, /./)
  await make(page, 'Posters', /Jazz night poster/, /./)
  await make(page, 'Documents', /Project proposal/, /./)
  await make(page, 'Slides', /Quarterly review/, /./)
  await make(page, 'Audio', /Podcast intro/, /./)
  await home(page)
  const recent = page.getByRole('list', { name: 'Recent conversations' }).getByRole('listitem')
  expect(await recent.count()).toBeGreaterThanOrEqual(5)
  await page.getByRole('button', { name: 'Library', exact: true }).first().click()
  const made = page.getByRole('list', { name: 'Made' }).getByRole('listitem')
  await expect(made.first()).toBeVisible()
  expect(await made.count()).toBeGreaterThanOrEqual(5)
  // The Library holds them, not this browser: a fresh one, signed in as the same person, lists them.
  const fresh = await (await browser.newContext({ baseURL: test.info().project.use.baseURL })).newPage()
  await signIn(fresh)
  await fresh.getByRole('button', { name: 'Library', exact: true }).first().click()
  const again = fresh.getByRole('list', { name: 'Made' }).getByRole('listitem')
  await expect(again.first()).toBeVisible()
  expect(await again.count()).toBeGreaterThanOrEqual(5)
})

test('a file this browser kept before the Library is carried up to it on the next visit, and dropped here', async ({ browser, request }) => {
  const token = await bearer(request, USER, PASS, ORG)
  const sub = (JSON.parse(Buffer.from(token.split('.')[1]!, 'base64url').toString()) as { sub: string }).sub
  const key = `files/image/gate${Date.now().toString(36)}/m1/v1/kept.png`
  const ctx = await browser.newContext({ baseURL: test.info().project.use.baseURL })
  // What an earlier Studio left: the bytes in this browser's database, no Library item.
  await ctx.addInitScript(
    ({ name, key, host }) => {
      if (location.hostname !== host || sessionStorage.getItem('seeded')) return
      sessionStorage.setItem('seeded', '1')
      const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC'), (c) => c.charCodeAt(0))
      const req = indexedDB.open(name, 1)
      req.onupgradeneeded = () => req.result.createObjectStore('files', { keyPath: 'key' })
      req.onsuccess = () => req.result.transaction('files', 'readwrite').objectStore('files').put({ key, blob: new Blob([png], { type: 'image/png' }), mime: 'image/png', modified: Date.now() })
    },
    { name: `hanzo-studio:${ORG}:${sub}`, key, host: new URL(test.info().project.use.baseURL!).hostname },
  )
  const page = await ctx.newPage()
  await signIn(page)
  const record = () =>
    page.evaluate(
      ({ name, key }) =>
        new Promise<{ blob: boolean; lib: string }>((resolve) => {
          const req = indexedDB.open(name, 1)
          req.onsuccess = () => {
            const get = req.result.transaction('files', 'readonly').objectStore('files').get(key)
            get.onsuccess = () => resolve({ blob: Boolean(get.result?.blob), lib: String(get.result?.lib ?? '') })
          }
        }),
      { name: `hanzo-studio:${ORG}:${sub}`, key },
    )
  await expect.poll(record, { timeout: 60_000 }).toEqual({ blob: false, lib: expect.stringMatching(/^lib_/) })
  const { lib } = await record()
  const item = await request.get(`${API}/v1/library/${lib}`, { headers: { Authorization: `Bearer ${token}`, 'X-Org-Id': ORG } })
  expect(item.status()).toBe(200)
  expect(((await item.json()) as { origin?: string }).origin).toMatch(/^\/studio\/c\/gate/)
  await request.delete(`${API}/v1/library/${lib}`, { headers: { Authorization: `Bearer ${token}`, 'X-Org-Id': ORG } })
})

test('what one person keeps, another member of the org and another org cannot see', async ({ request }) => {
  test.skip(!OTHER.user || !OTHER.pass || !OTHER.org, 'IAM_OTHER_USER, IAM_OTHER_PASS and IAM_OTHER_ORG name a second account')
  const mine = await shelf(request, await bearer(request, USER, PASS, ORG), ORG)
  expect(mine.status).toBe(200)
  expect(mine.ids.length).toBeGreaterThan(0)
  const theirs = await bearer(request, OTHER.user, OTHER.pass, OTHER.org)
  for (const org of [ORG, OTHER.org]) {
    const seen = await shelf(request, theirs, org)
    expect(seen.status, `the other account lists the Library in ${org}`).toBe(200)
    expect(seen.ids.filter((id) => mine.ids.includes(id))).toEqual([])
    const one = await request.get(`${API}/v1/library/${mine.ids[0]}`, { headers: { Authorization: `Bearer ${theirs}`, 'X-Org-Id': org } })
    expect(one.status(), `one item by id, in ${org}`).toBe(404)
  }
})

test('at 390 wide nothing scrolls sideways, and axe finds nothing serious', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await signIn(page)
  const wide = await page.evaluate(() => document.scrollingElement!.scrollWidth - window.innerWidth)
  expect(wide).toBeLessThanOrEqual(0)
  const axe = await new AxeBuilder({ page }).analyze()
  const bad = axe.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
  expect(bad.map((v) => `${v.id}: ${v.nodes.length}`)).toEqual([])
})
