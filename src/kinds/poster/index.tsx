/**
 * Posters: a fixed size (A-series, Letter, 18x24, 24x36, social and story),
 * a picture from the image lane, words from the chat, laid out in the chosen
 * type system and drawn in the browser. A version keeps its words, layout and
 * picture as its spec and the PNG as its file; the PDF is drawn from the same
 * SVG on export, at the page's physical size.
 *
 * Without a picture (none asked for, or the image lane could not draw one) a
 * poster is set in type alone and the turn says why.
 */
import { SizableText, XStack, YStack } from '@hanzo/gui'
import { Frame } from '@hanzogui/lucide-icons-2'
import { useEffect, useState } from 'react'

import { current, filename, type Made, type Stored } from '../../made.ts'
import { data, page, pixels } from '../../raster.ts'
import { Choice, Row, Text1, TextN } from '../../ui.tsx'
import { read, structured, url, type Wire } from '../../wire.ts'
import { picture } from '../image.tsx'
import { num, str, type Kind, type ViewProps, type Work } from '../kind.ts'
import { PRESETS, settle, type TypeSystem } from '../type/system.ts'
import { aspect, compose, format, FORMATS, LAYOUT_NAMES, LAYOUTS, type Layout, type Poster } from './compose.ts'

export interface Spec {
  format: string
  layout: Layout
  headline: string
  subhead: string
  details: string
  cta: string
  /** The picture: a stored file's key, or '' for none. */
  picture: string
  /** What the picture was drawn from, to draw it again. */
  prompt: string
  system: TypeSystem
  [k: string]: unknown
}

const spec = (v: Record<string, unknown>): Spec => {
  const s = v as Partial<Spec>
  return {
    format: FORMATS.some((f) => f.id === s.format) ? s.format! : 'a3',
    layout: (LAYOUTS as readonly string[]).includes(s.layout ?? '') ? s.layout! : 'hero',
    headline: str(s.headline, 'Untitled'),
    subhead: str(s.subhead),
    details: typeof s.details === 'string' ? s.details : '',
    cta: str(s.cta),
    picture: str(s.picture),
    prompt: str(s.prompt),
    system: settle((s.system ?? {}) as Partial<TypeSystem>, PRESETS[0]),
  }
}

/** The SVG for a spec: its picture read back from the store. */
export async function svg(wire: Wire, s: Spec): Promise<string> {
  const pic = s.picture ? await data(await read(wire, s.picture)) : ''
  const p: Poster = { ...s, picture: pic }
  return compose(p)
}

/** Draw a spec and keep its PNG. */
async function draw(w: Work, s: Spec): Promise<Stored[]> {
  w.doing('Laying out the poster')
  const f = format(s.format)
  const png = await pixels(await svg(w.wire, s), f.width, f.height)
  return [await w.keep('poster.png', png, { width: f.width, height: f.height })]
}

/** A picture for a poster: from a tab, else drawn from words; '' and the reason when none can be had. */
async function art(w: Work, s: Spec, from: number): Promise<{ key: string; why: string }> {
  const tab = from ? w.find(from) : null
  if (tab) {
    const f = current(tab).files.find((x) => x.mime.startsWith('image/'))
    if (f) return { key: f.key, why: '' }
  }
  if (!s.prompt) return { key: '', why: '' }
  try {
    const got = await picture(w, { prompt: s.prompt, aspect: aspect(format(s.format), s.layout), model: '' })
    return { key: got.files[0]?.key ?? '', why: '' }
  } catch (e) {
    return { key: '', why: e instanceof Error ? e.message : String(e) }
  }
}

const CHANGE = {
  type: 'object',
  additionalProperties: false,
  required: ['headline', 'subhead', 'details', 'cta', 'layout', 'format', 'picture'],
  properties: {
    headline: { type: 'string' },
    subhead: { type: 'string' },
    details: { type: 'string' },
    cta: { type: 'string' },
    layout: { type: 'string', enum: LAYOUTS },
    format: { type: 'string', enum: FORMATS.map((f) => f.id) },
    picture: { type: 'string', description: 'An instruction to edit the picture, a new description to draw it again prefixed with "new:", or "" to keep it.' },
  },
} as const

export const poster: Kind = {
  id: 'poster',
  label: 'Poster',
  blurb: 'Print and social posters: a picture, a headline, the details, laid out in your type.',
  icon: Frame,
  tool: {
    name: 'make_poster',
    description:
      'Make a poster or flyer: a picture with a headline, a subhead, details (date, place, price) and a call to action, laid out at a print or social size in the chosen type system.',
    parameters: {
      type: 'object',
      properties: {
        title: { type: 'string', description: 'A short name for the tab.' },
        headline: { type: 'string', description: 'The headline, short and strong: two to seven words.' },
        subhead: { type: 'string', description: 'One supporting line.' },
        details: { type: 'string', description: 'Lines separated by newlines: date and time, place, price.' },
        cta: { type: 'string', description: 'A call to action of one to three words, or empty.' },
        format: { type: 'string', enum: FORMATS.map((f) => f.id), description: 'a3 for print unless asked; story for stories; square or portrait for social feeds.' },
        layout: { type: 'string', enum: LAYOUTS, description: Object.entries(LAYOUT_NAMES).map(([k, v]) => `${k}: ${v}`).join('; ') },
        image_prompt: { type: 'string', description: 'A full visual description of the picture to draw, with no words in it. Empty for a type-only poster.' },
        image_tab: { type: 'integer', description: 'A tab whose picture to use instead of drawing one.' },
      },
      required: ['title', 'headline'],
    },
  },
  needs: () => null,
  make: async (w, args) => {
    const s = spec({
      format: str(args.format, 'a3'),
      layout: str(args.layout, 'hero'),
      headline: str(args.headline),
      subhead: str(args.subhead),
      details: str(args.details),
      cta: str(args.cta),
      prompt: str(args.image_prompt),
      system: w.brand ?? PRESETS[0],
    })
    const got = await art(w, s, num(args.image_tab, 0))
    s.picture = got.key
    const files = await draw(w, s)
    const note = got.why ? `Set in type alone. ${got.why}` : s.headline
    return { title: str(args.title, s.headline), draft: { spec: s, files, note, by: 'Studio layout' } }
  },
  revise: async (w, made, change) => {
    const now = spec(current(made).spec)
    let next: Spec = { ...now, ...(change.patch ?? {}) } as Spec
    let why = ''
    if (change.instruction) {
      w.doing('Rewriting the poster')
      const ask = await structured<Record<string, string>>(
        w.wire,
        w.writer,
        'You revise posters. Given the poster as JSON and an instruction, answer the whole poster with the instruction applied. Keep everything the instruction does not ask to change exactly as it is. picture: "" keeps the picture; a plain instruction ("warmer", "remove the people") edits it; "new: <description>" draws a new one.',
        `Poster: ${JSON.stringify({ headline: now.headline, subhead: now.subhead, details: now.details, cta: now.cta, layout: now.layout, format: now.format, picture: '' })}\nInstruction: ${change.instruction}`,
        CHANGE,
        w.signal,
      )
      next = spec({ ...now, ...ask, picture: now.picture, prompt: now.prompt, system: w.brand ?? now.system })
      const p = str(ask.picture)
      if (p.startsWith('new:')) {
        next.prompt = p.slice(4).trim()
        const got = await art(w, { ...next, picture: '' }, 0)
        next.picture = got.key || now.picture
        why = got.why
      } else if (p && now.picture) {
        try {
          const f = current(made).files[0]
          const src: Stored = { key: now.picture, mime: f?.mime ?? 'image/png', name: 'picture' }
          const got = await picture(w, { prompt: p, aspect: aspect(format(next.format), next.layout), model: '', from: [src] })
          next.picture = got.files[0]?.key ?? now.picture
        } catch (e) {
          why = e instanceof Error ? e.message : String(e)
        }
      }
    }
    const files = await draw(w, next)
    return { spec: next, files, note: why ? `${change.instruction}. The picture is unchanged: ${why}` : (change.instruction ?? 'Edited'), by: 'Studio layout' }
  },
  View: Sheet,
  exports: [
    {
      label: 'PNG',
      run: async (w, m, v) => ({ name: filename(m.title, 'png'), blob: await read(w, v.files[0]!.key) }),
    },
    {
      label: 'PDF (print size)',
      run: async (w, m, v) => {
        const s = spec(v.spec)
        const f = format(s.format)
        return { name: filename(m.title, 'pdf'), blob: await page(await svg(w, s), f.width, f.height, f.points, m.title) }
      },
    },
    {
      label: 'SVG',
      run: async (w, m, v) => ({ name: filename(m.title, 'svg'), blob: new Blob([await svg(w, spec(v.spec))], { type: 'image/svg+xml' }) }),
    },
  ],
}

function Sheet({ made, version, wire, edit, busy, others }: ViewProps) {
  const s = spec(version.spec)
  const [draft, setDraft] = useState(s)
  useEffect(() => setDraft(spec(version.spec)), [version.id])
  const [preview, setPreview] = useState('')
  const changed = JSON.stringify({ ...draft, system: null }) !== JSON.stringify({ ...s, system: null })
  // The shown version's PNG, or a live draw of the words being edited.
  useEffect(() => {
    let live = true
    let made = ''
    const run = async () => {
      const text = changed ? await svg(wire, draft) : ''
      if (!live) return
      if (text) {
        made = URL.createObjectURL(new Blob([text], { type: 'image/svg+xml' }))
        setPreview(made)
      } else {
        const f = version.files[0]
        if (f) setPreview(await url(wire, f.key))
      }
    }
    const t = setTimeout(() => void run().catch(() => undefined), changed ? 250 : 0)
    return () => {
      live = false
      clearTimeout(t)
      if (made) URL.revokeObjectURL(made)
    }
  }, [wire, version.id, changed, JSON.stringify(draft)])
  const pictures = others.flatMap((m: Made) =>
    m.kind === 'image' ? current(m).files.filter((f) => f.mime.startsWith('image/')).map((f, i) => ({ value: f.key, label: `${m.title}${i ? ` ${i + 1}` : ''}` })) : [],
  )
  const set = (patch: Partial<Spec>) => setDraft({ ...draft, ...patch })
  const f = format(draft.format)
  return (
    <YStack flex={1} minH={0} $md={{ flexDirection: 'row' }}>
      <YStack flex={1} minH={240} items="center" justify="center" p="$3" bg="$hover">
        {preview ? (
          <img src={preview} alt={made.title} style={{ maxWidth: '100%', maxHeight: '100%', aspectRatio: `${f.width} / ${f.height}`, objectFit: 'contain', boxShadow: '0 8px 30px rgba(0,0,0,.35)' }} />
        ) : (
          <SizableText color="$soft">Drawing…</SizableText>
        )}
      </YStack>
      <YStack width="100%" $md={{ width: 300 }} gap="$3" p="$3" overflow="scroll" borderLeftWidth={1} borderColor="$borderColor">
        <Row label="Headline">
          <TextN label="Headline" rows={2} value={draft.headline} onChange={(headline) => set({ headline })} />
        </Row>
        <Row label="Subhead">
          <Text1 label="Subhead" value={draft.subhead} onChange={(subhead) => set({ subhead })} />
        </Row>
        <Row label="Details">
          <TextN label="Details" value={draft.details} onChange={(details) => set({ details })} />
        </Row>
        <Row label="Call to action">
          <Text1 label="Call to action" value={draft.cta} onChange={(cta) => set({ cta })} />
        </Row>
        <Row label="Layout">
          <Choice label="Layout" value={draft.layout} options={LAYOUTS.map((l) => ({ value: l, label: LAYOUT_NAMES[l] }))} onChange={(layout) => set({ layout: layout as Layout })} />
        </Row>
        <Row label="Size">
          <Choice label="Size" value={draft.format} options={FORMATS.map((x) => ({ value: x.id, label: x.label }))} onChange={(format) => set({ format })} />
        </Row>
        <Row label="Picture">
          <Choice label="Picture" value={draft.picture} options={[{ value: '', label: 'None (type only)' }, ...pictures, ...(draft.picture && !pictures.some((p) => p.value === draft.picture) ? [{ value: draft.picture, label: 'This poster’s picture' }] : [])]} onChange={(picture) => set({ picture })} />
        </Row>
        <XStack gap="$2">
          <XStack
            render="button"
            aria-label="Save these changes as a new version"
            disabled={!changed || busy}
            onPress={() => changed && !busy && edit(draft, 'Edited in place')}
            flex={1}
            height={36}
            items="center"
            justify="center"
            rounded="$3"
            bg={changed ? '$ink' : '$hover'}
            cursor={changed ? 'pointer' : 'default'}
          >
            <SizableText size="$2" color={changed ? '$background' : '$soft'}>
              {busy ? 'Saving…' : 'Save version'}
            </SizableText>
          </XStack>
          {changed ? (
            <XStack render="button" aria-label="Discard changes" onPress={() => setDraft(s)} height={36} px="$3" items="center" rounded="$3" borderWidth={1} borderColor="$borderColor" cursor="pointer">
              <SizableText size="$2">Discard</SizableText>
            </XStack>
          ) : null}
        </XStack>
      </YStack>
    </YStack>
  )
}
