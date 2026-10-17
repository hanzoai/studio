import { afterEach, describe, expect, it, vi } from 'vitest'

import { draw, Refusal, type Wire } from './wire.ts'

const w: Wire = { api: 'https://api.test', engine: '', token: () => 't', org: 'o' }

const answer = (body: object, head: Record<string, string>) =>
  vi.stubGlobal('fetch', async () => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json', ...head } }))

afterEach(() => vi.unstubAllGlobals())

describe('a picture the gateway withholds', () => {
  it('says the reason and what lifts it', async () => {
    answer({ model: 'enso-free', choices: [{ message: { content: 'An apple, in words.' } }] }, {
      'X-Hanzo-Usage-Reason': 'paid_plan_required',
      'X-Hanzo-Upgrade-Url': 'https://hanzo.ai/pay/cart?plan=dev',
    })
    const e = await draw(w, 'google/gemini-3.1-flash-image', 'an apple').catch((x: unknown) => x)
    expect(e).toBeInstanceOf(Refusal)
    expect((e as Refusal).status).toBe(402)
    expect((e as Refusal).code).toBe('paid_plan_required')
    expect((e as Refusal).message).toBe('Pictures from this model need a paid plan: https://hanzo.ai/pay/cart?plan=dev')
  })

  it('is a bad gateway when nothing says why', async () => {
    answer({ choices: [{ message: { content: 'words' } }] }, {})
    const e = (await draw(w, 'm', 'x').catch((x: unknown) => x)) as Refusal
    expect(e.status).toBe(502)
    expect(e.message).toBe('m answered with no picture.')
  })
})
