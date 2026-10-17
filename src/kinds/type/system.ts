/**
 * A TYPE SYSTEM: the faces, the scale, the weights, the leading and the colours
 * everything else is drawn in. It is the Brand chip: a poster, a document, a
 * deck or an ad made while one is chosen takes its faces and colours from it.
 *
 * The presets are Studio's design systems as they were (Modernist, Classical,
 * Nocturne, Organic, Broadsheet, Industry) with open faces in place of the
 * commercial ones they named, and Hanzo, the design system Studio ships (Zen).
 *
 * Sizes are not stored: a scale is a base and a ratio, and step n is
 * base x ratio^n, rounded to the pixel. Three exports read the same object:
 * CSS custom properties, design tokens as JSON, and a @hanzo/gui theme.
 */
import { FAMILIES, font, stack } from './fonts.ts'

export interface Colors {
  ink: string
  paper: string
  accent: string
  muted: string
  /** A second accent, for charts and tags. */
  second: string
}

export interface TypeSystem {
  name: string
  blurb: string
  display: string
  text: string
  mono: string
  scale: { base: number; ratio: number }
  weights: { display: number; heading: number; body: number; strong: number }
  leading: { display: number; heading: number; body: number }
  /** Letter-spacing, em: display sizes and all-caps labels. */
  tracking: { display: number; caps: number }
  colors: Colors
}

/** The named steps, smallest first, and the power of the ratio each is. */
export const STEPS = [
  ['caption', -2],
  ['small', -1],
  ['body', 0],
  ['lead', 1],
  ['h4', 2],
  ['h3', 3],
  ['h2', 4],
  ['h1', 5],
  ['display', 7],
] as const

export type Step = (typeof STEPS)[number][0]

export function size(t: TypeSystem, step: Step): number {
  const n = STEPS.find((s) => s[0] === step)![1]
  return Math.round(t.scale.base * t.scale.ratio ** n)
}

/** The ratios a scale may take, by their musical names. */
export const RATIOS = [
  [1.125, 'Major second'],
  [1.2, 'Minor third'],
  [1.25, 'Major third'],
  [1.333, 'Perfect fourth'],
  [1.414, 'Augmented fourth'],
  [1.5, 'Perfect fifth'],
  [1.618, 'Golden'],
] as const

const sys = (s: TypeSystem) => s

export const PRESETS: readonly TypeSystem[] = [
  sys({
    name: 'Hanzo',
    blurb: 'The Hanzo design system: Zen throughout, black and white, one bright edge.',
    display: 'Zen',
    text: 'Zen',
    mono: 'Zen Mono',
    scale: { base: 16, ratio: 1.25 },
    weights: { display: 220, heading: 606, body: 497, strong: 650 },
    leading: { display: 1.0, heading: 1.15, body: 1.55 },
    tracking: { display: -0.03, caps: 0.08 },
    colors: { ink: '#ededed', paper: '#0a0a0a', accent: '#ffffff', muted: '#8a8a8a', second: '#6aa5ff' },
  }),
  sys({
    name: 'Modernist',
    blurb: 'Swiss grid, generous whitespace, a single decisive accent.',
    display: 'Inter',
    text: 'Inter',
    mono: 'JetBrains Mono',
    scale: { base: 16, ratio: 1.333 },
    weights: { display: 800, heading: 700, body: 400, strong: 600 },
    leading: { display: 0.95, heading: 1.1, body: 1.5 },
    tracking: { display: -0.04, caps: 0.1 },
    colors: { ink: '#0a0a0a', paper: '#f4f4f4', accent: '#e8442a', muted: '#6e6e6e', second: '#1f4fd1' },
  }),
  sys({
    name: 'Classical',
    blurb: 'Serif headlines, warm neutrals, editorial hairlines.',
    display: 'Cormorant Garamond',
    text: 'EB Garamond',
    mono: 'IBM Plex Mono',
    scale: { base: 18, ratio: 1.333 },
    weights: { display: 600, heading: 600, body: 400, strong: 600 },
    leading: { display: 1.05, heading: 1.15, body: 1.6 },
    tracking: { display: -0.01, caps: 0.12 },
    colors: { ink: '#1c1a17', paper: '#f2ead9', accent: '#8a6d3b', muted: '#7d7464', second: '#3f5a4a' },
  }),
  sys({
    name: 'Nocturne',
    blurb: 'Near-black canvas, luminous accents, high contrast.',
    display: 'Zen',
    text: 'Zen',
    mono: 'Zen Mono',
    scale: { base: 16, ratio: 1.414 },
    weights: { display: 300, heading: 600, body: 450, strong: 650 },
    leading: { display: 1.0, heading: 1.12, body: 1.55 },
    tracking: { display: -0.025, caps: 0.1 },
    colors: { ink: '#ededed', paper: '#0a0a0b', accent: '#6aa5ff', muted: '#8b8b93', second: '#b48cff' },
  }),
  sys({
    name: 'Organic',
    blurb: 'Earthy palette, soft forms, humanist type.',
    display: 'Fraunces',
    text: 'Work Sans',
    mono: 'IBM Plex Mono',
    scale: { base: 17, ratio: 1.25 },
    weights: { display: 700, heading: 600, body: 400, strong: 600 },
    leading: { display: 1.02, heading: 1.15, body: 1.6 },
    tracking: { display: -0.02, caps: 0.08 },
    colors: { ink: '#2b2a24', paper: '#efe7d6', accent: '#8fae74', muted: '#857d6c', second: '#d99a6c' },
  }),
  sys({
    name: 'Broadsheet',
    blurb: 'Newsprint columns, tight leading, ink black.',
    display: 'Playfair Display',
    text: 'Source Serif 4',
    mono: 'IBM Plex Mono',
    scale: { base: 17, ratio: 1.5 },
    weights: { display: 900, heading: 700, body: 400, strong: 700 },
    leading: { display: 0.98, heading: 1.08, body: 1.45 },
    tracking: { display: -0.015, caps: 0.14 },
    colors: { ink: '#141414', paper: '#f7f5f0', accent: '#c0392b', muted: '#6b6b6b', second: '#2c3e50' },
  }),
  sys({
    name: 'Industry',
    blurb: 'Utilitarian grotesk and mono, safety accents, hard edges.',
    display: 'Archivo',
    text: 'IBM Plex Sans',
    mono: 'IBM Plex Mono',
    scale: { base: 15, ratio: 1.333 },
    weights: { display: 900, heading: 700, body: 400, strong: 600 },
    leading: { display: 0.92, heading: 1.05, body: 1.5 },
    tracking: { display: -0.01, caps: 0.16 },
    colors: { ink: '#e6e8ea', paper: '#101215', accent: '#ffcf4a', muted: '#8b9096', second: '#ff6b3d' },
  }),
]

const HEX = /^#[0-9a-f]{6}$/i

/** A type system as the model or a person wrote it, made safe to draw: unknown faces, bad colours and wild numbers fall back. */
export function settle(v: Partial<TypeSystem> & Record<string, unknown>, base: TypeSystem = PRESETS[0]!): TypeSystem {
  const pick = (f: unknown, or: string) => (typeof f === 'string' && FAMILIES.includes(f) ? f : or)
  const c = (v.colors ?? {}) as Partial<Colors>
  const color = (x: unknown, or: string) => (typeof x === 'string' && HEX.test(x) ? x.toLowerCase() : or)
  const n = (x: unknown, or: number, lo: number, hi: number) => (typeof x === 'number' && Number.isFinite(x) ? Math.min(hi, Math.max(lo, x)) : or)
  const w = (v.weights ?? {}) as Partial<TypeSystem['weights']>
  const l = (v.leading ?? {}) as Partial<TypeSystem['leading']>
  const t = (v.tracking ?? {}) as Partial<TypeSystem['tracking']>
  const s = (v.scale ?? {}) as Partial<TypeSystem['scale']>
  const display = pick(v.display, base.display)
  const text = pick(v.text, base.text)
  const mono = font(pick(v.mono, base.mono)).role === 'mono' ? pick(v.mono, base.mono) : base.mono
  return {
    name: typeof v.name === 'string' && v.name.trim() ? v.name.trim().slice(0, 60) : base.name,
    blurb: typeof v.blurb === 'string' ? v.blurb.trim().slice(0, 200) : base.blurb,
    display,
    text,
    mono,
    scale: { base: Math.round(n(s.base, base.scale.base, 12, 24)), ratio: n(s.ratio, base.scale.ratio, 1.067, 1.618) },
    weights: {
      display: Math.round(n(w.display, base.weights.display, 100, 900)),
      heading: Math.round(n(w.heading, base.weights.heading, 100, 900)),
      body: Math.round(n(w.body, base.weights.body, 100, 900)),
      strong: Math.round(n(w.strong, base.weights.strong, 100, 900)),
    },
    leading: {
      display: n(l.display, base.leading.display, 0.8, 1.4),
      heading: n(l.heading, base.leading.heading, 0.9, 1.6),
      body: n(l.body, base.leading.body, 1.2, 2.0),
    },
    tracking: { display: n(t.display, base.tracking.display, -0.1, 0.1), caps: n(t.caps, base.tracking.caps, 0, 0.3) },
    colors: {
      ink: color(c.ink, base.colors.ink),
      paper: color(c.paper, base.colors.paper),
      accent: color(c.accent, base.colors.accent),
      muted: color(c.muted, base.colors.muted),
      second: color(c.second, base.colors.second),
    },
  }
}

/** The JSON Schema a model fills to write one. */
export const SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['name', 'blurb', 'display', 'text', 'mono', 'scale', 'weights', 'leading', 'tracking', 'colors'],
  properties: {
    name: { type: 'string' },
    blurb: { type: 'string' },
    display: { type: 'string', enum: FAMILIES },
    text: { type: 'string', enum: FAMILIES },
    mono: { type: 'string', enum: FAMILIES.filter((f) => font(f).role === 'mono') },
    scale: {
      type: 'object',
      additionalProperties: false,
      required: ['base', 'ratio'],
      properties: { base: { type: 'number' }, ratio: { type: 'number' } },
    },
    weights: {
      type: 'object',
      additionalProperties: false,
      required: ['display', 'heading', 'body', 'strong'],
      properties: { display: { type: 'number' }, heading: { type: 'number' }, body: { type: 'number' }, strong: { type: 'number' } },
    },
    leading: {
      type: 'object',
      additionalProperties: false,
      required: ['display', 'heading', 'body'],
      properties: { display: { type: 'number' }, heading: { type: 'number' }, body: { type: 'number' } },
    },
    tracking: {
      type: 'object',
      additionalProperties: false,
      required: ['display', 'caps'],
      properties: { display: { type: 'number' }, caps: { type: 'number' } },
    },
    colors: {
      type: 'object',
      additionalProperties: false,
      required: ['ink', 'paper', 'accent', 'muted', 'second'],
      properties: {
        ink: { type: 'string' },
        paper: { type: 'string' },
        accent: { type: 'string' },
        muted: { type: 'string' },
        second: { type: 'string' },
      },
    },
  },
} as const

/** What a writer is told: the faces it may use, and the rules a pairing keeps. */
export const BRIEF = `You design type systems. Answer with one JSON object.
Faces: choose display, text and mono ONLY from this list (each is SIL OFL 1.1): ${FAMILIES.join(', ')}.
Pair with contrast: a characterful display face with a quiet text face, or one family used at two weights. Mono is for code, data and labels.
scale.base is the body size in px (14 to 20); scale.ratio is one of 1.125, 1.2, 1.25, 1.333, 1.414, 1.5, 1.618 (larger for posters and display-led brands, smaller for dense documents).
Weights are CSS weights 100 to 900 the faces carry. Leading is unitless line-height: display 0.9 to 1.1, heading 1.05 to 1.25, body 1.4 to 1.7. Tracking is em: display -0.05 to 0, caps 0.05 to 0.2.
Colours are #rrggbb: ink on paper must read at WCAG AA (contrast 4.5:1 or better); accent is the one decisive colour; muted is for secondary text and must read on paper at 3:1 or better; second is a supporting accent.
name is two words at most; blurb is one sentence on the feeling.`

/** WCAG relative luminance contrast of two #rrggbb colours. */
export function contrast(a: string, b: string): number {
  const lum = (hex: string) => {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
    return 0.2126 * c[0]! + 0.7152 * c[1]! + 0.0722 * c[2]!
  }
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p) as [number, number]
  return (x + 0.05) / (y + 0.05)
}

/** CSS custom properties: the one export every web surface reads. */
export function css(t: TypeSystem): string {
  const lines = [
    `/* ${t.name}: ${t.blurb} */`,
    ':root {',
    `  --font-display: ${stack(t.display)};`,
    `  --font-text: ${stack(t.text)};`,
    `  --font-mono: ${stack(t.mono)};`,
    ...STEPS.map(([s]) => `  --text-${s}: ${size(t, s)}px;`),
    `  --weight-display: ${t.weights.display};`,
    `  --weight-heading: ${t.weights.heading};`,
    `  --weight-body: ${t.weights.body};`,
    `  --weight-strong: ${t.weights.strong};`,
    `  --leading-display: ${t.leading.display};`,
    `  --leading-heading: ${t.leading.heading};`,
    `  --leading-body: ${t.leading.body};`,
    `  --tracking-display: ${t.tracking.display}em;`,
    `  --tracking-caps: ${t.tracking.caps}em;`,
    ...Object.entries(t.colors).map(([k, v]) => `  --color-${k}: ${v};`),
    '}',
  ]
  return lines.join('\n') + '\n'
}

/** Design tokens in the W3C Design Tokens format ($value / $type). */
export function tokens(t: TypeSystem): string {
  const doc = {
    $description: `${t.name}: ${t.blurb}`,
    font: {
      display: { $type: 'fontFamily', $value: [t.display, font(t.display).role === 'serif' ? 'serif' : 'sans-serif'] },
      text: { $type: 'fontFamily', $value: [t.text, font(t.text).role === 'serif' ? 'serif' : 'sans-serif'] },
      mono: { $type: 'fontFamily', $value: [t.mono, 'monospace'] },
    },
    size: Object.fromEntries(STEPS.map(([s]) => [s, { $type: 'dimension', $value: `${size(t, s)}px` }])),
    weight: Object.fromEntries(Object.entries(t.weights).map(([k, v]) => [k, { $type: 'fontWeight', $value: v }])),
    leading: Object.fromEntries(Object.entries(t.leading).map(([k, v]) => [k, { $type: 'number', $value: v }])),
    tracking: Object.fromEntries(Object.entries(t.tracking).map(([k, v]) => [k, { $type: 'dimension', $value: `${v}em` }])),
    color: Object.fromEntries(Object.entries(t.colors).map(([k, v]) => [k, { $type: 'color', $value: v }])),
  }
  return JSON.stringify(doc, null, 2) + '\n'
}

/** A @hanzo/gui theme module: the faces as gui fonts, the colours as a theme. */
export function gui(t: TypeSystem): string {
  const sizes = Object.fromEntries(STEPS.map(([s], i) => [i + 1, size(t, s)]))
  const lineHeights = Object.fromEntries(STEPS.map(([s], i) => [i + 1, Math.round(size(t, s) * (i >= 6 ? t.leading.display : i >= 3 ? t.leading.heading : t.leading.body))]))
  const fontOf = (family: string, weight: number) =>
    `createFont({\n    family: ${JSON.stringify(stack(family))},\n    size: ${JSON.stringify(sizes)},\n    lineHeight: ${JSON.stringify(lineHeights)},\n    weight: { 4: '${weight}' },\n    letterSpacing: { 4: 0 },\n  })`
  return `// ${t.name}: ${t.blurb}
// A @hanzo/gui theme: pass \`fonts\` and \`themes\` to createGui alongside your config.
import { createFont } from '@hanzo/gui'

export const fonts = {
  heading: ${fontOf(t.display, t.weights.heading)},
  body: ${fontOf(t.text, t.weights.body)},
  mono: ${fontOf(t.mono, t.weights.body)},
}

export const themes = {
  ${JSON.stringify(t.name.toLowerCase().replace(/[^a-z0-9]+/g, '_'))}: {
    background: '${t.colors.paper}',
    color: '${t.colors.ink}',
    colorMuted: '${t.colors.muted}',
    accent: '${t.colors.accent}',
    accentSecond: '${t.colors.second}',
    borderColor: '${t.colors.muted}',
  },
}
`
}
