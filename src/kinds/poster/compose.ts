/**
 * A poster, laid out: a size, a layout, the words and a picture, drawn in a
 * type system, as one SVG that carries its faces and its picture inside it.
 * The same SVG is the preview, the PNG and the PDF, so what is shown is what
 * is shipped.
 *
 * Words are set, not dropped in: the headline takes the largest size at which
 * it fits its box in at most its lines, measured in the face it is drawn in;
 * every other block wraps at its measure and stops at its line limit.
 */
import { face, stack, use } from '../type/fonts.ts'
import type { TypeSystem } from '../type/system.ts'

export interface Format {
  id: string
  label: string
  /** Pixels drawn. */
  width: number
  height: number
  /** The page, in points, for the PDF. */
  points: [number, number]
}

const mm = (v: number) => (v * 72) / 25.4

export const FORMATS: readonly Format[] = [
  { id: 'a4', label: 'A4', width: 1240, height: 1754, points: [mm(210), mm(297)] },
  { id: 'a3', label: 'A3', width: 1754, height: 2480, points: [mm(297), mm(420)] },
  { id: 'a2', label: 'A2', width: 2480, height: 3508, points: [mm(420), mm(594)] },
  { id: 'letter', label: 'US Letter', width: 1275, height: 1650, points: [612, 792] },
  { id: '18x24', label: '18 × 24 in', width: 1800, height: 2400, points: [1296, 1728] },
  { id: '24x36', label: '24 × 36 in', width: 1800, height: 2700, points: [1728, 2592] },
  { id: 'square', label: 'Social 1:1', width: 1080, height: 1080, points: [810, 810] },
  { id: 'portrait', label: 'Social 4:5', width: 1080, height: 1350, points: [810, 1012.5] },
  { id: 'story', label: 'Story 9:16', width: 1080, height: 1920, points: [810, 1440] },
  { id: 'landscape', label: 'Landscape 16:9', width: 1920, height: 1080, points: [1440, 810] },
]

export const format = (id: string): Format => FORMATS.find((f) => f.id === id) ?? FORMATS[0]!

/** The aspect a picture for this format is drawn at (the image lane's names). */
export function aspect(f: Format, layout: Layout): string {
  const r = layout === 'split' ? f.width / (f.height * 0.58) : f.width / f.height
  const named: [string, number][] = [
    ['1:1', 1],
    ['4:5', 0.8],
    ['3:4', 0.75],
    ['2:3', 0.667],
    ['9:16', 0.5625],
    ['16:9', 1.778],
    ['3:2', 1.5],
    ['4:3', 1.333],
    ['21:9', 2.333],
  ]
  return named.reduce((best, n) => (Math.abs(n[1] - r) < Math.abs(best[1] - r) ? n : best))[0]
}

export const LAYOUTS = ['hero', 'center', 'split', 'frame', 'type'] as const
export type Layout = (typeof LAYOUTS)[number]

export const LAYOUT_NAMES: Record<Layout, string> = {
  hero: 'Full bleed, words low',
  center: 'Full bleed, words centred',
  split: 'Picture over a colour block',
  frame: 'Framed picture on paper',
  type: 'Type only',
}

export interface Words {
  headline: string
  subhead: string
  /** Lines: a date, a place, a price. */
  details: string
  cta: string
}

export interface Poster extends Words {
  format: string
  /** Pixels, for a size no format names (an ad unit). */
  size?: { width: number; height: number }
  layout: Layout
  /** A data: URL of the picture, or '' for none. */
  picture: string
  system: TypeSystem
}

const xml = (s: string) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' })[c]!)

let measurer: CanvasRenderingContext2D | null = null

function width(text: string, family: string, weight: number, px: number, tracking: number): number {
  measurer ??= document.createElement('canvas').getContext('2d')
  if (!measurer) return text.length * px * 0.55
  measurer.font = `${weight} ${px}px ${stack(family)}`
  return measurer.measureText(text).width + Math.max(0, text.length - 1) * tracking * px
}

/** Words into lines no wider than `max`. A word wider than the line stands alone on one. */
function wrap(text: string, max: number, family: string, weight: number, px: number, tracking: number): string[] {
  const out: string[] = []
  for (const para of text.split('\n')) {
    let line = ''
    for (const word of para.split(/\s+/).filter(Boolean)) {
      const next = line ? `${line} ${word}` : word
      if (line && width(next, family, weight, px, tracking) > max) {
        out.push(line)
        line = word
      } else line = next
    }
    if (line) out.push(line)
  }
  return out
}

/**
 * The largest size, between `lo` and `hi`, at which `text` sets in at most
 * `lines` lines of `max`, standing no taller than `tall` at `leading`.
 */
function fit(text: string, max: number, lines: number, family: string, weight: number, tracking: number, lo: number, hi: number, tall = Infinity, leading = 1): number {
  let a = lo
  let b = hi
  for (let i = 0; i < 14; i++) {
    const m = (a + b) / 2
    const set = wrap(text, max, family, weight, m, tracking)
    const ok = set.length <= lines && set.every((l) => width(l, family, weight, m, tracking) <= max) && m * (1 + (set.length - 1) * leading) <= tall
    if (ok) a = m
    else b = m
  }
  return Math.floor(a)
}

interface Block {
  lines: string[]
  px: number
  family: string
  weight: number
  leading: number
  tracking: number
  fill: string
  upper?: boolean
}

function block(b: Block, x: number, y: number, anchor: 'start' | 'middle' = 'start'): string {
  if (!b.lines.length) return ''
  const spans = b.lines.map((l, i) => `<tspan x="${x}" dy="${i === 0 ? 0 : b.px * b.leading}">${xml(b.upper ? l.toUpperCase() : l)}</tspan>`).join('')
  return `<text x="${x}" y="${y + b.px * 0.8}" text-anchor="${anchor}" font-family="${xml(stack(b.family))}" font-weight="${b.weight}" font-size="${b.px}" letter-spacing="${(b.tracking * b.px).toFixed(2)}" fill="${b.fill}">${spans}</text>`
}

const height = (b: Block) => (b.lines.length ? b.px * (1 + (b.lines.length - 1) * b.leading) : 0)

/** Load the faces a poster draws in, so measuring and drawing use them. */
export async function ready(t: TypeSystem): Promise<void> {
  use(t.display)
  use(t.text)
  await Promise.all([
    document.fonts.load(`${t.weights.display} 64px ${stack(t.display)}`),
    document.fonts.load(`${t.weights.body} 32px ${stack(t.text)}`),
    document.fonts.load(`${t.weights.strong} 32px ${stack(t.text)}`),
  ]).catch(() => undefined)
}

/** The poster as SVG, faces and picture inside it. */
export async function compose(p: Poster): Promise<string> {
  const f = p.size ?? format(p.format)
  const t = p.system
  const c = t.colors
  await ready(t)
  const W = f.width
  const H = f.height
  if (W / H > 3 || W / H < 0.4) return banner(p, W, H)
  const u = Math.min(W, H) / 100
  const margin = Math.round(u * 6.5)
  const measure = W - margin * 2
  const layout: Layout = p.picture ? p.layout : 'type'
  const dark = layout === 'hero' || layout === 'center'
  const ink = dark ? '#ffffff' : c.ink
  const soft = dark ? 'rgba(255,255,255,0.82)' : c.muted

  const headWidth = layout === 'center' || layout === 'type' ? measure : measure * (W > H ? 0.6 : 0.92)
  const headLines = layout === 'type' ? 5 : 3
  const headMax = layout === 'type' ? u * 30 : u * 17
  // The headline's room: what the layout leaves once the margins, the details and the call to action are placed.
  const headTall = { hero: H * 0.42, center: H * 0.5, split: H * 0.42 - margin * 2 - u * 8, frame: H * 0.3, type: H - margin * 2 - u * 6 - u * 12 }[layout]
  const headPx = fit(p.headline || ' ', headWidth, headLines, t.display, t.weights.display, t.tracking.display, u * 4, headMax, headTall, t.leading.display)
  const head: Block = {
    lines: wrap(p.headline, headWidth, t.display, t.weights.display, headPx, t.tracking.display),
    px: headPx,
    family: t.display,
    weight: t.weights.display,
    leading: t.leading.display,
    tracking: t.tracking.display,
    fill: ink,
  }
  const subPx = Math.round(Math.max(u * 2.6, Math.min(u * 4.4, headPx * 0.34)))
  const sub: Block = {
    lines: wrap(p.subhead, headWidth, t.text, t.weights.body, subPx, 0).slice(0, 3),
    px: subPx,
    family: t.text,
    weight: t.weights.body,
    leading: t.leading.body,
    tracking: 0,
    fill: soft,
  }
  const detPx = Math.round(u * 2.4)
  const det: Block = {
    lines: wrap(p.details, measure * 0.7, t.text, t.weights.strong, detPx, t.tracking.caps).slice(0, 4),
    px: detPx,
    family: t.text,
    weight: t.weights.strong,
    leading: 1.35,
    tracking: t.tracking.caps,
    fill: ink,
    upper: true,
  }
  const ctaPx = Math.round(u * 2.6)
  const cta = p.cta.trim()
  const ctaW = cta ? width(cta.toUpperCase(), t.text, t.weights.strong, ctaPx, t.tracking.caps) + ctaPx * 2.4 : 0
  const ctaH = ctaPx * 2.4
  const ctaFill = layout === 'type' || layout === 'frame' || layout === 'split' ? c.accent : '#ffffff'
  const ctaInk = readable(ctaFill)
  const pill = (x: number, y: number, anchor: 'start' | 'middle' = 'start') => {
    if (!cta) return ''
    const left = anchor === 'middle' ? x - ctaW / 2 : x
    return `<rect x="${left}" y="${y}" width="${ctaW}" height="${ctaH}" rx="${ctaH / 2}" fill="${ctaFill}"/>${block({ lines: [cta], px: ctaPx, family: t.text, weight: t.weights.strong, leading: 1, tracking: t.tracking.caps, fill: ctaInk, upper: true }, left + ctaW / 2, y + ctaH / 2 - ctaPx * 0.62, 'middle')}`
  }
  const gap = u * 2.2
  const words = (x: number, y: number, anchor: 'start' | 'middle' = 'start') => {
    let at = y
    let out = block(head, x, at, anchor)
    at += height(head) + (sub.lines.length ? gap : 0)
    out += block(sub, x, at, anchor)
    at += height(sub)
    return { out, at }
  }
  const wordsHeight = height(head) + (sub.lines.length ? gap + height(sub) : 0)
  const footHeight = Math.max(height(det), cta ? ctaH : 0)

  let body = ''
  const pic = (x: number, y: number, w: number, h: number, round = 0) =>
    `<clipPath id="pic"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${round}"/></clipPath><image href="${p.picture}" x="${x}" y="${y}" width="${w}" height="${h}" preserveAspectRatio="xMidYMid slice" clip-path="url(#pic)"/>`

  if (layout === 'hero') {
    body += pic(0, 0, W, H)
    body += `<linearGradient id="scrim" x1="0" y1="0" x2="0" y2="1"><stop offset="0.35" stop-color="#000" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity="0.78"/></linearGradient><rect width="${W}" height="${H}" fill="url(#scrim)"/>`
    const top = H - margin - footHeight - gap * 2 - wordsHeight
    body += words(margin, top).out
    body += block(det, margin, H - margin - height(det))
    body += pill(W - margin - ctaW, H - margin - ctaH)
  } else if (layout === 'center') {
    body += pic(0, 0, W, H)
    body += `<rect width="${W}" height="${H}" fill="#000" fill-opacity="0.42"/>`
    const top = (H - wordsHeight) / 2
    body += words(W / 2, top, 'middle').out
    body += block({ ...det, lines: det.lines }, W / 2, H - margin - height(det) - (cta ? ctaH + gap : 0), 'middle')
    body += pill(W / 2, H - margin - ctaH, 'middle')
  } else if (layout === 'split') {
    const ph = Math.round(H * 0.58)
    body += `<rect width="${W}" height="${H}" fill="${c.paper}"/>`
    body += pic(0, 0, W, ph)
    body += `<rect x="0" y="${ph}" width="${W}" height="${u * 0.8}" fill="${c.accent}"/>`
    body += words(margin, ph + margin).out
    body += block(det, margin, H - margin - height(det))
    body += pill(W - margin - ctaW, H - margin - ctaH)
  } else if (layout === 'frame') {
    body += `<rect width="${W}" height="${H}" fill="${c.paper}"/>`
    const w = words(margin, margin)
    body += w.out
    const top = w.at + gap * 1.5
    const bottom = H - margin - footHeight - gap * 1.5
    body += pic(margin, top, measure, Math.max(u * 10, bottom - top), u * 1.6)
    body += block(det, margin, H - margin - height(det))
    body += pill(W - margin - ctaW, H - margin - ctaH)
  } else {
    body += `<rect width="${W}" height="${H}" fill="${c.paper}"/>`
    body += `<rect x="${margin}" y="${margin}" width="${u * 14}" height="${u * 1.4}" fill="${c.accent}"/>`
    body += words(margin, margin + u * 6).out
    body += block(det, margin, H - margin - height(det))
    body += pill(W - margin - ctaW, H - margin - ctaH)
  }

  const faces = (await Promise.all([...new Set([t.display, t.text])].map((fam) => face(fam).catch(() => '')))).join('\n')
  return `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><defs><style><![CDATA[${faces}]]></style></defs>${body}</svg>`
}

/**
 * An ad unit too long or too tall for a poster's layouts (728x90, 320x50,
 * 160x600): the picture at one end, the headline in the middle, the call to
 * action at the other.
 */
async function banner(p: Poster, W: number, H: number): Promise<string> {
  const t = p.system
  const c = t.colors
  const wide = W > H
  const pad = Math.round(Math.min(W, H) * (wide ? 0.14 : 0.08))
  const cta = p.cta.trim()
  const ctaPx = Math.round(wide ? H * 0.2 : W * 0.085)
  const ctaW = cta ? width(cta.toUpperCase(), t.text, t.weights.strong, ctaPx, t.tracking.caps) + ctaPx * 2 : 0
  const ctaH = ctaPx * 2.2
  const ink = readable(c.paper) === '#ffffff' ? '#ffffff' : c.ink
  let body = `<rect width="${W}" height="${H}" fill="${c.paper}"/>`
  let x = pad
  let y = pad
  let room = wide ? W - pad * 2 : H - pad * 2
  if (p.picture) {
    const pw = wide ? Math.round(H * 1.25) : W
    const ph = wide ? H : Math.round(W * 0.9)
    body += `<clipPath id="pic"><rect width="${pw}" height="${ph}"/></clipPath><image href="${p.picture}" width="${pw}" height="${ph}" preserveAspectRatio="xMidYMid slice" clip-path="url(#pic)"/>`
    if (wide) {
      x = pw + pad
      room -= pw
    } else {
      y = ph + pad
      room -= ph
    }
  }
  const across = wide ? room - (cta ? ctaW + pad : 0) : W - pad * 2
  const lines = wide ? (H < 70 ? 1 : 2) : 4
  const px = fit(p.headline || ' ', across, lines, t.display, t.weights.display, t.tracking.display, 8, wide ? H * 0.42 : W * 0.16)
  const set = wrap(p.headline, across, t.display, t.weights.display, px, t.tracking.display)
  const tall = px * (1 + (set.length - 1) * t.leading.display)
  const top = wide ? (H - tall) / 2 : y
  body += block({ lines: set, px, family: t.display, weight: t.weights.display, leading: t.leading.display, tracking: t.tracking.display, fill: ink }, x, top)
  if (cta) {
    const cx = wide ? W - pad - ctaW : pad
    const cy = wide ? (H - ctaH) / 2 : H - pad - ctaH
    body += `<rect x="${cx}" y="${cy}" width="${ctaW}" height="${ctaH}" rx="${ctaH / 2}" fill="${c.accent}"/>`
    body += block({ lines: [cta], px: ctaPx, family: t.text, weight: t.weights.strong, leading: 1, tracking: t.tracking.caps, fill: readable(c.accent), upper: true }, cx + ctaW / 2, cy + ctaH / 2 - ctaPx * 0.62, 'middle')
  }
  const faces = (await Promise.all([...new Set([t.display, t.text])].map((fam) => face(fam).catch(() => '')))).join('\n')
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><defs><style><![CDATA[${faces}]]></style></defs>${body}</svg>`
}

/** Black or white, whichever reads on `bg`. */
function readable(bg: string): string {
  const v = parseInt(bg.slice(1), 16)
  const l = 0.299 * ((v >> 16) & 255) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255)
  return l > 150 ? '#0a0a0a' : '#ffffff'
}
