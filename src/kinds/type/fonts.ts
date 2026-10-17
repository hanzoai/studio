/**
 * The faces a type system may pair: Zen (the Hanzo family, @hanzo/font) and a
 * short list of open fonts. Every one is SIL OFL 1.1, read from its own OFL.txt
 * in github.com/google/fonts on 2026-10-16 (`licence` is that file), so a
 * poster, a document or a deck drawn in any of them is free to ship.
 *
 * A face is loaded in the page by its stylesheet, and drawn into a picture
 * (a poster's PNG, its PDF) by its bytes: `face()` reads the woff2 the
 * stylesheet names and hands it back as a data: URL an SVG can embed.
 */

export type Role = 'sans' | 'serif' | 'display' | 'mono'

export interface Font {
  family: string
  role: Role
  /** The weights it carries; a variable face carries the range. */
  weights: readonly number[]
  italic: boolean
  /** The stylesheet that loads it; '' for Zen, which the host already loads. */
  css: string
  licence: string
}

const OFL = (dir: string) => `https://github.com/google/fonts/blob/main/ofl/${dir}/OFL.txt`
const G = (q: string) => `https://fonts.googleapis.com/css2?family=${q}&display=swap`

export const FONTS: readonly Font[] = [
  { family: 'Zen', role: 'sans', weights: [100, 200, 300, 400, 500, 600, 700, 800, 900], italic: true, css: '', licence: 'https://www.npmjs.com/package/@hanzo/font (LICENSE.txt, SIL OFL 1.1)' },
  { family: 'Zen Mono', role: 'mono', weights: [100, 200, 300, 400, 500, 600, 700, 800, 900], italic: false, css: '', licence: 'https://www.npmjs.com/package/@hanzo/font (LICENSE.txt, SIL OFL 1.1)' },
  { family: 'Inter', role: 'sans', weights: [400, 500, 600, 700, 800], italic: true, css: G('Inter:ital,wght@0,400..800;1,400..800'), licence: OFL('inter') },
  { family: 'Manrope', role: 'sans', weights: [400, 500, 600, 700, 800], italic: false, css: G('Manrope:wght@400..800'), licence: OFL('manrope') },
  { family: 'DM Sans', role: 'sans', weights: [400, 500, 600, 700], italic: true, css: G('DM+Sans:ital,wght@0,400..700;1,400..700'), licence: OFL('dmsans') },
  { family: 'Work Sans', role: 'sans', weights: [400, 500, 600, 700], italic: true, css: G('Work+Sans:ital,wght@0,400..700;1,400..700'), licence: OFL('worksans') },
  { family: 'IBM Plex Sans', role: 'sans', weights: [400, 500, 600, 700], italic: true, css: G('IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400'), licence: OFL('ibmplexsans') },
  { family: 'Space Grotesk', role: 'display', weights: [400, 500, 600, 700], italic: false, css: G('Space+Grotesk:wght@400..700'), licence: OFL('spacegrotesk') },
  { family: 'Archivo', role: 'display', weights: [400, 500, 600, 700, 800, 900], italic: true, css: G('Archivo:ital,wght@0,400..900;1,400..900'), licence: OFL('archivo') },
  { family: 'Bricolage Grotesque', role: 'display', weights: [400, 500, 600, 700, 800], italic: false, css: G('Bricolage+Grotesque:wght@400..800'), licence: OFL('bricolagegrotesque') },
  { family: 'Syne', role: 'display', weights: [400, 500, 600, 700, 800], italic: false, css: G('Syne:wght@400..800'), licence: OFL('syne') },
  { family: 'Fraunces', role: 'serif', weights: [400, 500, 600, 700, 800, 900], italic: true, css: G('Fraunces:ital,wght@0,400..900;1,400..900'), licence: OFL('fraunces') },
  { family: 'Playfair Display', role: 'serif', weights: [400, 500, 600, 700, 800, 900], italic: true, css: G('Playfair+Display:ital,wght@0,400..900;1,400..900'), licence: OFL('playfairdisplay') },
  { family: 'DM Serif Display', role: 'serif', weights: [400], italic: true, css: G('DM+Serif+Display:ital@0;1'), licence: OFL('dmserifdisplay') },
  { family: 'Instrument Serif', role: 'serif', weights: [400], italic: true, css: G('Instrument+Serif:ital@0;1'), licence: OFL('instrumentserif') },
  { family: 'Cormorant Garamond', role: 'serif', weights: [400, 500, 600, 700], italic: true, css: G('Cormorant+Garamond:ital,wght@0,400..700;1,400..700'), licence: OFL('cormorantgaramond') },
  { family: 'EB Garamond', role: 'serif', weights: [400, 500, 600, 700, 800], italic: true, css: G('EB+Garamond:ital,wght@0,400..800;1,400..800'), licence: OFL('ebgaramond') },
  { family: 'Source Serif 4', role: 'serif', weights: [400, 500, 600, 700], italic: true, css: G('Source+Serif+4:ital,wght@0,400..700;1,400..700'), licence: OFL('sourceserif4') },
  { family: 'IBM Plex Mono', role: 'mono', weights: [400, 500, 600], italic: true, css: G('IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;1,400'), licence: OFL('ibmplexmono') },
  { family: 'JetBrains Mono', role: 'mono', weights: [400, 500, 600, 700], italic: true, css: G('JetBrains+Mono:ital,wght@0,400..700;1,400..700'), licence: OFL('jetbrainsmono') },
]

export const FAMILIES: readonly string[] = FONTS.map((f) => f.family)

export const font = (family: string): Font => FONTS.find((f) => f.family === family) ?? FONTS[0]!

/** A CSS font-family value with a fallback of its own role. */
export function stack(family: string): string {
  const f = font(family)
  const fall = f.role === 'serif' ? 'Georgia, serif' : f.role === 'mono' ? 'ui-monospace, monospace' : 'system-ui, sans-serif'
  return `"${f.family}", ${fall}`
}

const linked = new Set<string>()

/** Load a face in this page, once. */
export function use(family: string): void {
  const f = font(family)
  if (!f.css || linked.has(f.css) || typeof document === 'undefined') return
  linked.add(f.css)
  const link = document.createElement('link')
  link.rel = 'stylesheet'
  link.href = f.css
  document.head.appendChild(link)
}

const embedded = new Map<string, Promise<string>>()

/** The @font-face rules for a face with its bytes inline, for an SVG that is drawn to a picture. */
export function face(family: string): Promise<string> {
  const f = font(family)
  let hit = embedded.get(f.family)
  if (!hit) {
    hit = (async () => {
      if (!f.css) return zen(f.family)
      const css = await (await fetch(f.css)).text()
      // The Latin subset only: the blocks a stylesheet marks /* latin */.
      const blocks = css.split('/* ').filter((b) => b.startsWith('latin */'))
      const out: string[] = []
      for (const b of blocks.length ? blocks : [css]) {
        const src = /url\((https:[^)]+\.woff2)\)/.exec(b)?.[1]
        if (!src) continue
        const data = await inline(src, 'font/woff2')
        out.push(b.replace(/^latin \*\//, '').replace(/url\(https:[^)]+\.woff2\)/, `url(${data})`))
      }
      return out.join('\n')
    })()
    embedded.set(f.family, hit)
    hit.catch(() => embedded.delete(f.family))
  }
  return hit
}

/** Zen's faces as this page loaded them (@hanzo/font/css), found in its stylesheets. */
async function zen(family: string): Promise<string> {
  const rules: string[] = []
  for (const sheet of Array.from(document.styleSheets)) {
    let list: CSSRuleList
    try {
      list = sheet.cssRules
    } catch {
      continue
    }
    for (const r of Array.from(list)) {
      if (r instanceof CSSFontFaceRule && r.style.getPropertyValue('font-family').replace(/["']/g, '').trim() === family) {
        const src = /url\(["']?([^"')]+\.woff2)["']?\)/.exec(r.style.getPropertyValue('src'))?.[1]
        if (!src) continue
        const abs = new URL(src, sheet.href ?? document.baseURI).href
        rules.push(r.cssText.replace(/url\(["']?[^"')]+\.woff2["']?\)/, `url(${await inline(abs, 'font/woff2')})`))
      }
    }
  }
  return rules.join('\n')
}

async function inline(url: string, mime: string): Promise<string> {
  const buf = new Uint8Array(await (await fetch(url)).arrayBuffer())
  let bin = ''
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
  return `data:${mime};base64,${btoa(bin)}`
}
