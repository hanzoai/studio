/**
 * Studio, live, as a signed-in person: sign-in through IAM with the hanzo-studio
 * client (PKCE), the home's starters, things made end to end (each a tab, then in
 * Recent and the Library), a phone's width, and axe.
 *
 * The sign-in page itself (hanzo.ai/login) is another surface's; this signs the
 * account in with the same request that page answers (POST /v1/iam/login for the
 * code Studio asked for) and returns the code to Studio's own callback, so the
 * PKCE exchange, the callback and the session are Studio's real ones.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

const USER = process.env.IAM_USER ?? ''
const PASS = process.env.IAM_PASS ?? ''
const ORG = process.env.IAM_ORG ?? ''
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

test('makes a type system, a poster, a document, slides and a voice, each a tab, then in Recent and the Library', async ({ page }) => {
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
