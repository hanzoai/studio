/**
 * Documents and slides, written by the chat and drawn by the engine's
 * renderers (PDF, Word, PowerPoint): one kind for prose (`doc`) and one for
 * decks (`deck`), made the same way.
 *
 *   1. The writer answers the document as the engine's own IR, against the
 *      grammar the engine publishes (GET /v1/documents/schema), with picture
 *      blocks that say what to draw or which tab to take.
 *   2. Each picture is drawn (the image lane) or taken from its tab and kept;
 *      the IR holds the stored key.
 *   3. At render each key's picture goes inside the IR as a data: URL, the
 *      brand becomes the IR's `theme`, and the engine draws the files, which are
 *      kept here: a version's files never depend on the engine keeping them.
 */
import { SizableText, XStack, YStack } from '@hanzo/gui'
import { FileText, Presentation } from '@hanzogui/lucide-icons-2'
import { useEffect, useState } from 'react'

import { current, filename, type Made } from '../made.ts'
import { Act } from '../ui.tsx'
import { shrink } from '../raster.ts'
import { documents, read, structured, type Wire } from '../wire.ts'
import { picture } from './image.tsx'
import { num, str, type Kind, type ViewProps, type Work } from './kind.ts'
import { PRESETS, type TypeSystem } from './type/system.ts'

type Arch = 'doc' | 'deck'

interface Block {
  /** A doc block's type; a slide has a layout instead. */
  type?: string
  [k: string]: unknown
}

interface Ir {
  title: string
  blocks?: Block[]
  slides?: Block[]
  theme?: Record<string, string>
}

/** The brand as the engine's theme. */
export function theme(t: TypeSystem | null): Record<string, string> {
  const s = t ?? PRESETS[0]!
  return { ink: s.colors.ink, paper: s.colors.paper, accent: s.colors.accent, muted: s.colors.muted, display_font: s.display, text_font: s.text }
}

const BLOCK = {
  type: 'object',
  additionalProperties: false,
  required: ['type', 'text', 'level', 'items', 'headers', 'rows', 'prompt', 'tab', 'caption'],
  properties: {
    type: { type: 'string', enum: ['title', 'h', 'p', 'ul', 'table', 'kv', 'hr', 'image'] },
    text: { type: 'string' },
    level: { type: 'integer' },
    items: { type: 'array', items: { type: 'string' } },
    headers: { type: 'array', items: { type: 'string' } },
    rows: { type: 'array', items: { type: 'array', items: { type: 'string' } } },
    prompt: { type: 'string', description: 'image: what to draw, with no words in it' },
    tab: { type: 'integer', description: 'image: a tab whose picture to use, 0 for none' },
    caption: { type: 'string' },
  },
} as const

const SLIDE = {
  type: 'object',
  additionalProperties: false,
  required: ['layout', 'title', 'subtitle', 'bullets', 'notes', 'prompt', 'tab', 'quote', 'labels', 'values'],
  properties: {
    layout: { type: 'string', enum: ['title', 'section', 'bullets', 'image', 'split', 'quote', 'chart', 'closing'] },
    title: { type: 'string' },
    subtitle: { type: 'string' },
    bullets: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
    prompt: { type: 'string', description: 'image and split: what to draw, with no words in it' },
    tab: { type: 'integer', description: 'image and split: a tab whose picture to use, 0 for none' },
    quote: { type: 'string' },
    labels: { type: 'array', items: { type: 'string' }, description: 'chart: the categories' },
    values: { type: 'array', items: { type: 'number' }, description: 'chart: one value per label' },
  },
} as const

const schema = (a: Arch) => ({
  type: 'object',
  additionalProperties: false,
  required: ['title', a === 'doc' ? 'blocks' : 'slides'],
  properties: { title: { type: 'string' }, [a === 'doc' ? 'blocks' : 'slides']: { type: 'array', items: a === 'doc' ? BLOCK : SLIDE } },
})

const BRIEF: Record<Arch, string> = {
  doc: `You write documents as JSON blocks: title {text, caption as the subtitle}, h {text, level 1-3}, p {text}, ul {items}, table {headers, rows}, kv {rows as [key, value] pairs}, hr, image {prompt or tab, caption}. Fill every field of every block; use "" , 0 and [] for fields a block does not use. Write the real content, complete and specific, in the person's language. Use an image block only where a picture helps (a cover, a product, a place), at most three.`,
  deck: `You write slide decks as JSON slides. Layouts: title (title, subtitle), section (title), bullets (title, 3 to 5 bullets), image (title, prompt or tab), split (title, bullets, prompt or tab), quote (quote, subtitle as the attribution), chart (title, labels, values), closing (title, subtitle). Fill every field of every slide; use "", 0 and [] for fields a layout does not use. Speaker notes in notes. Ten slides or fewer unless asked. Pictures with no words in them.`,
}

/** Draw or take each picture a written IR asks for, and keep it: the IR then names stored keys. */
async function pictures(w: Work, ir: Ir, arch: Arch): Promise<Ir> {
  const list = (arch === 'doc' ? ir.blocks : ir.slides) ?? []
  let n = 0
  const out: Block[] = []
  for (const b of list) {
    const wants = arch === 'doc' ? b.type === 'image' : b.layout === 'image' || b.layout === 'split'
    if (!wants || str(b.src)) {
      out.push(b)
      continue
    }
    n++
    const tab = num(b.tab, 0) ? w.find(num(b.tab, 0)) : null
    const f = tab ? current(tab).files.find((x) => x.mime.startsWith('image/')) : undefined
    if (f) {
      out.push({ ...b, src: f.key })
      continue
    }
    if (!str(b.prompt)) {
      out.push({ ...b, src: '' })
      continue
    }
    try {
      w.doing(`Drawing picture ${n}`)
      const got = await picture(w, { prompt: str(b.prompt), aspect: arch === 'deck' ? (b.layout === 'split' ? '4:5' : '16:9') : '3:2', model: '' })
      out.push({ ...b, src: got.files[0]?.key ?? '' })
    } catch (e) {
      // A picture the lane cannot draw is left out, and said: the words still ship.
      out.push({ ...b, src: '', missing: e instanceof Error ? e.message : String(e) })
    }
  }
  return arch === 'doc' ? { ...ir, blocks: out } : { ...ir, slides: out }
}

const texts = (v: unknown): string[] => (Array.isArray(v) ? v.map((x) => String(x ?? '')).filter((x) => x.trim()) : [])

/** A written block in the engine's own words (GET /v1/documents/schema): the block's tag is `t`. */
function block(b: Block, src: string): Block | null {
  switch (b.type) {
    case 'title':
      return { t: 'title', text: str(b.text), sub: str(b.caption) }
    case 'h':
      return { t: 'h', text: str(b.text), level: num(b.level, 2, 1, 3) }
    case 'p':
      return { t: 'p', text: str(b.text) }
    case 'ul':
      return { t: 'ul', items: texts(b.items) }
    case 'table':
      return { t: 'table', headers: texts(b.headers), rows: Array.isArray(b.rows) ? (b.rows as unknown[]).map(texts) : [], align: [], foot: [] }
    case 'kv':
      return { t: 'kv', pairs: Array.isArray(b.rows) ? (b.rows as unknown[]).map(texts).filter((r) => r.length >= 2).map((r) => [r[0], r.slice(1).join(' ')]) : [], cols: 1 }
    case 'hr':
      return { t: 'hr' }
    case 'image':
      return src ? { t: 'image', src, caption: str(b.caption) } : null
    default:
      return null
  }
}

/** A written slide in the engine's own words. A picture slide with no picture falls back to words. */
function slide(b: Block, src: string): Block {
  const layout = str(b.layout, 'bullets')
  const words = { title: str(b.title), subtitle: str(b.subtitle), bullets: texts(b.bullets), notes: str(b.notes) }
  if ((layout === 'image' || layout === 'split') && !src) return { layout: layout === 'split' ? 'bullets' : 'section', ...words }
  if (layout === 'chart') {
    const labels = texts(b.labels)
    const values = Array.isArray(b.values) ? (b.values as unknown[]).map((v) => num(v, 0)) : []
    return labels.length && labels.length === values.length ? { layout, ...words, chart: { labels, values } } : { layout: 'bullets', ...words }
  }
  return { layout, ...words, ...(src ? { image: src } : {}), ...(layout === 'quote' ? { quote: str(b.quote) } : {}) }
}

/** The IR the engine is handed: stored keys as read addresses, pictures that could not be had dropped, the brand as theme. */
async function wired(wire: Wire, ir: Ir, arch: Arch, brand: TypeSystem | null): Promise<Ir> {
  const list = (arch === 'doc' ? ir.blocks : ir.slides) ?? []
  const out: Block[] = []
  for (const b of list) {
    const key = str(b.src)
    // The picture travels inside the IR: what is kept is this person's alone, and no address of it is handed out.
    const src = key ? await shrink(await read(wire, key)).catch(() => '') : ''
    const next = arch === 'doc' ? block(b, src) : slide(b, src)
    if (next) out.push(next)
  }
  return { ...(arch === 'doc' ? { blocks: out } : { slides: out }), title: ir.title, theme: theme(brand) }
}

/** Render on the engine and keep the files. */
async function render(w: Work, ir: Ir, arch: Arch, brand: TypeSystem | null) {
  w.doing(arch === 'doc' ? 'Setting the document' : 'Building the slides')
  const doc = await documents.create(w.wire, { kind: arch, ir: await wired(w.wire, ir, arch, brand), title: ir.title })
  const formats = arch === 'doc' ? (['pdf', 'docx'] as const) : (['pdf', 'pptx'] as const)
  const files = []
  for (const f of formats) {
    w.doing(`Writing the ${f.toUpperCase()}`)
    const blob = await documents.file(w.wire, doc.id, f)
    files.push(await w.keep(filename(ir.title, f), blob))
  }
  return { id: doc.id, files }
}

function kind(arch: Arch): Kind {
  const id = arch === 'doc' ? 'document' : 'slides'
  return {
    id,
    label: arch === 'doc' ? 'Document' : 'Slides',
    blurb: arch === 'doc' ? 'Reports, proposals, letters, one-pagers, as PDF and Word.' : 'Decks with pictures and charts, as PowerPoint and PDF.',
    icon: arch === 'doc' ? FileText : Presentation,
    tool: {
      name: arch === 'doc' ? 'make_document' : 'make_slides',
      description:
        arch === 'doc'
          ? 'Write a document: a report, proposal, letter, memo, resume, invoice, one-pager, press release, contract. Rendered to PDF and Word in the brand.'
          : 'Write a slide deck: a pitch, a business review, a project update, a talk, a lesson. Rendered to PowerPoint and PDF in the brand.',
      parameters: {
        type: 'object',
        properties: {
          title: { type: 'string', description: 'The document\'s title.' },
          brief: { type: 'string', description: 'Everything the person said it should contain, and who it is for.' },
        },
        required: ['title', 'brief'],
      },
    },
    needs: () => null,
    make: async (w, args) => {
      w.doing(arch === 'doc' ? 'Writing the document' : 'Writing the slides')
      const brief = str(args.brief)
      const written = await structured<Ir>(w.wire, w.writer, BRIEF[arch], `Title: ${str(args.title)}\n${brief}`, schema(arch), w.signal)
      const ir = await pictures(w, { ...written, title: str(written.title, str(args.title, 'Untitled')) }, arch)
      const { id: engine, files } = await render(w, ir, arch, w.brand)
      const missing = ((arch === 'doc' ? ir.blocks : ir.slides) ?? []).map((b) => str(b.missing)).filter(Boolean)
      const note = missing.length ? `${brief.slice(0, 120)} (pictures left out: ${missing[0]})` : brief.slice(0, 160)
      return { title: ir.title, draft: { spec: { ir, engine, brief }, files, note, by: w.writer } }
    },
    revise: async (w, made, change) => {
      const now = current(made).spec as { ir: Ir; brief: string }
      let ir: Ir = change.patch?.ir ? (change.patch.ir as Ir) : now.ir
      if (change.instruction) {
        w.doing('Revising')
        const written = await structured<Ir>(
          w.wire,
          w.writer,
          `${BRIEF[arch]}\nYou are revising: answer the whole document with the instruction applied, and keep everything it does not ask to change exactly as it is (keep each picture block's src).`,
          `Current: ${JSON.stringify(now.ir)}\nInstruction: ${change.instruction}`,
          schema(arch),
          w.signal,
        )
        ir = await pictures(w, written, arch)
      }
      const { id: engine, files } = await render(w, ir, arch, w.brand)
      return { spec: { ir, engine, brief: now.brief }, files, note: change.instruction ?? 'Edited', by: change.instruction ? w.writer : 'edit' }
    },
    View: (p) => <Pages {...p} arch={arch} />,
    exports: (arch === 'doc' ? ['pdf', 'docx'] : ['pdf', 'pptx']).map((ext) => ({
      label: ext === 'pdf' ? 'PDF' : ext === 'docx' ? 'Word (.docx)' : 'PowerPoint (.pptx)',
      run: async (wire: Wire, m: Made, v) => {
        const f = v.files.find((x) => x.name.endsWith(`.${ext}`))
        if (!f) throw new Error(`This version has no ${ext.toUpperCase()}.`)
        return { name: filename(m.title, ext), blob: await read(wire, f.key) }
      },
    })),
  }
}

export const document_ = kind('doc')
export const slides = kind('deck')

/** The PDF, in the browser's own viewer; the words beside it for an edit in place. */
function Pages({ made, version, wire, edit, busy, arch }: ViewProps & { arch: Arch }) {
  const pdf = version.files.find((f) => f.name.endsWith('.pdf'))
  const [src, setSrc] = useState('')
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(false)
  useEffect(() => {
    if (!pdf) return
    let live = true
    let made = ''
    read(wire, pdf.key).then(
      (b) => {
        if (!live) return
        made = URL.createObjectURL(new Blob([b], { type: 'application/pdf' }))
        setSrc(made)
      },
      (e: unknown) => live && setError(e instanceof Error ? e.message : String(e)),
    )
    return () => {
      live = false
      if (made) URL.revokeObjectURL(made)
    }
  }, [wire, pdf?.key])
  const ir = (version.spec as { ir?: Ir }).ir
  return (
    <YStack flex={1} minH={0}>
      <XStack gap="$2" px="$3" py="$2" borderBottomWidth={1} borderColor="$borderColor" items="center">
        <SizableText size="$1" color="$soft" flex={1} numberOfLines={1}>
          {version.files.map((f) => f.name).join(' · ')}
        </SizableText>
        {ir ? (
          <Act label={editing ? 'Show the pages' : 'Edit the words'} onPress={() => setEditing(!editing)} on={editing}>
            <SizableText size="$2">{editing ? 'Pages' : 'Edit words'}</SizableText>
          </Act>
        ) : null}
      </XStack>
      {editing && ir ? (
        <Words ir={ir} arch={arch} busy={busy} onSave={(next) => edit({ ir: next }, 'Edited in place')} />
      ) : src ? (
        <iframe title={made.title} src={src} style={{ flex: 1, width: '100%', border: 0, minHeight: 0 }} />
      ) : (
        <YStack flex={1} items="center" justify="center">
          <SizableText color="$soft">{error || 'Opening…'}</SizableText>
        </YStack>
      )}
    </YStack>
  )
}

/** Every line of words the IR carries, as fields. */
function Words({ ir, arch, busy, onSave }: { ir: Ir; arch: Arch; busy: boolean; onSave: (ir: Ir) => void }) {
  const [draft, setDraft] = useState(ir)
  const list = (arch === 'doc' ? draft.blocks : draft.slides) ?? []
  const put = (i: number, patch: Record<string, unknown>) => {
    const next = list.map((b, j) => (j === i ? { ...b, ...patch } : b))
    setDraft(arch === 'doc' ? { ...draft, blocks: next } : { ...draft, slides: next })
  }
  const box = { width: '100%', padding: 8, borderRadius: 8, border: '1px solid var(--borderColor)', background: 'transparent', color: 'inherit', font: 'inherit' } as const
  return (
    <YStack flex={1} minH={0} overflow="scroll" p="$3" gap="$3">
      <input aria-label="Title" value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.currentTarget.value })} style={{ ...box, fontWeight: 600 }} />
      {list.map((b, i) => {
        const fields = (['text', 'title', 'subtitle', 'quote', 'caption'] as const).filter((k) => typeof b[k] === 'string' && (b[k] as string).length)
        const lists = (['items', 'bullets'] as const).filter((k) => Array.isArray(b[k]) && (b[k] as unknown[]).length)
        if (!fields.length && !lists.length) return null
        return (
          <YStack key={i} gap="$1.5">
            <SizableText size="$1" color="$soft">
              {arch === 'doc' ? String(b.type) : `Slide ${i + 1} · ${String(b.layout)}`}
            </SizableText>
            {fields.map((k) =>
              k === 'text' && b.type === 'p' ? (
                <textarea key={k} aria-label={`${k} ${i + 1}`} rows={4} value={b[k] as string} onChange={(e) => put(i, { [k]: e.currentTarget.value })} style={box} />
              ) : (
                <input key={k} aria-label={`${k} ${i + 1}`} value={b[k] as string} onChange={(e) => put(i, { [k]: e.currentTarget.value })} style={box} />
              ),
            )}
            {lists.map((k) => (
              <textarea
                key={k}
                aria-label={`${k} ${i + 1}, one per line`}
                rows={Math.max(2, (b[k] as string[]).length)}
                value={(b[k] as string[]).join('\n')}
                onChange={(e) => put(i, { [k]: e.currentTarget.value.split('\n') })}
                style={box}
              />
            ))}
          </YStack>
        )
      })}
      <XStack
        render="button"
        aria-label="Save these words as a new version"
        disabled={busy}
        onPress={() => !busy && onSave(draft)}
        height={36}
        items="center"
        justify="center"
        rounded="$3"
        bg="$ink"
        cursor="pointer"
      >
        <SizableText size="$2" color="$background">
          {busy ? 'Saving…' : 'Save version'}
        </SizableText>
      </XStack>
    </YStack>
  )
}
