import { describe, expect, it, vi } from 'vitest'

import type { Host } from '@hanzo/build'

import { nav, NEW } from './nav.tsx'

// The drawing libraries are not under test and do not load outside a bundler.
vi.mock('@hanzogui/lucide-icons-2', () => ({ Activity: () => null, LayoutTemplate: () => null, Library: () => null, SquarePen: () => null }))

describe('Studio’s places', () => {
  it('are one run with no heading over it: New creation, Explore, Library, Activity', () => {
    const groups = nav({ path: 'library' } as Host, () => undefined)
    expect(groups).toHaveLength(1)
    expect(groups[0]!.links.map((l) => l.label)).toEqual(['New creation', 'Explore', 'Library', 'Activity'])
    expect(groups[0]!.links[0]!.id).toBe(NEW)
    expect(groups[0]!.links.find((l) => l.active)?.id).toBe('library')
  })
})
