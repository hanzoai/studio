import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Wire } from '../wire.ts'
import { picture, UNDRAWN } from './image.tsx'
import type { Work } from './kind.ts'

// The drawing libraries are not under test and do not load outside a bundler.
vi.mock('@hanzogui/lucide-icons-2', () => ({ Image: () => null }))
vi.mock('@hanzo/gui', () => ({ SizableText: () => null, XStack: () => null, YStack: () => null }))

const wire: Wire = { api: 'https://api.test', engine: '', token: () => 't', org: 'o' }

const work = (missed: () => void): Work => ({
  wire,
  signal: new AbortController().signal,
  doing: () => undefined,
  missed,
  keep: async () => ({ key: 'k', mime: 'image/png', name: 'image-1.png' }),
  picture: async () => '',
  find: () => null,
  brand: null,
  fleet: null,
  models: [],
  writer: 'w',
})

const answer = (body: object, head: Record<string, string> = {}) =>
  vi.stubGlobal('fetch', async () => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json', ...head } }))

afterEach(() => vi.unstubAllGlobals())

describe('a picture the gateway does not draw', () => {
  it('is one plain sentence, never the model, and the turn is told to offer a retry', async () => {
    answer({ choices: [{ message: { content: 'words' } }] })
    const missed = vi.fn()
    const e = (await picture(work(missed), { prompt: 'an apple', aspect: '1:1', model: '' }).catch((x: unknown) => x)) as Error
    expect(e.message).toBe(UNDRAWN)
    expect(e.message).not.toMatch(/gemini|google|openai|answer/i)
    expect(missed).toHaveBeenCalledOnce()
  })

  it('says what a plan lifts, and offers no retry for it', async () => {
    answer({ choices: [{ message: { content: 'words' } }] }, { 'X-Hanzo-Usage-Reason': 'paid_plan_required', 'X-Hanzo-Upgrade-Url': 'https://hanzo.ai/pricing' })
    const missed = vi.fn()
    const e = (await picture(work(missed), { prompt: 'an apple', aspect: '1:1', model: '' }).catch((x: unknown) => x)) as Error
    expect(e.message).toBe('Pictures from this model need a paid plan: https://hanzo.ai/pricing')
    expect(missed).not.toHaveBeenCalled()
  })
})
