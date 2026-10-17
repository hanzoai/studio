import { describe, expect, it } from 'vitest'

import { contrast, css, gui, PRESETS, settle, size, tokens } from './system.ts'

describe('type systems', () => {
  it('keeps every preset readable: ink on paper at AA, muted at 3:1', () => {
    for (const p of PRESETS) {
      expect(contrast(p.colors.ink, p.colors.paper), p.name).toBeGreaterThanOrEqual(4.5)
      expect(contrast(p.colors.muted, p.colors.paper), p.name).toBeGreaterThanOrEqual(3)
    }
  })
  it('settles what a model writes: unknown faces, bad colours and wild numbers fall back', () => {
    const t = settle({ name: 'X', display: 'Comic Sans', text: 'Inter', mono: 'Inter', scale: { base: 90, ratio: 9 }, colors: { ink: 'red', paper: '#fff', accent: '#AABBCC', muted: '#123456', second: '#000000' } } as never)
    expect(t.display).toBe(PRESETS[0]!.display)
    expect(t.text).toBe('Inter')
    expect(t.mono).toBe(PRESETS[0]!.mono)
    expect(t.scale).toEqual({ base: 24, ratio: 1.618 })
    expect(t.colors.ink).toBe(PRESETS[0]!.colors.ink)
    expect(t.colors.paper).toBe(PRESETS[0]!.colors.paper)
    expect(t.colors.accent).toBe('#aabbcc')
  })
  it('derives sizes from base and ratio', () => {
    const t = { ...PRESETS[1]!, scale: { base: 16, ratio: 1.25 } }
    expect(size(t, 'body')).toBe(16)
    expect(size(t, 'lead')).toBe(20)
    expect(size(t, 'caption')).toBe(10)
  })
  it('exports the same values three ways', () => {
    const t = PRESETS[2]!
    expect(css(t)).toContain(`--text-body: ${size(t, 'body')}px;`)
    expect(css(t)).toContain(`--color-accent: ${t.colors.accent};`)
    const doc = JSON.parse(tokens(t))
    expect(doc.color.accent.$value).toBe(t.colors.accent)
    expect(doc.font.display.$value[0]).toBe(t.display)
    expect(gui(t)).toContain(`accent: '${t.colors.accent}'`)
    expect(gui(t)).toContain("import { createFont } from '@hanzo/gui'")
  })
})
