/**
 * Images: made from words, or edited from a picture with words.
 *
 * Two lanes, chosen per request: the gateway's image models (api.hanzo.ai,
 * billed to the org) and Zen Image on the org's own GPU (Z-Image-Turbo through
 * the engine), which is offered only while a GPU advertises `image.zen`. Auto
 * takes the GPU when one can run it, else the gateway. An edit is the same
 * choice: Qwen Edit on a GPU that advertises `edit.qwen` (the engine's fix
 * lane, from the picture staged on the engine), else a gateway model.
 */
import { SizableText, XStack, YStack } from '@hanzo/gui'
import { Image as Picture } from '@hanzogui/lucide-icons-2'
import { useState, useSyncExternalStore } from 'react'

import { render } from '../gpu.ts'
import { current, filename, type Made, type Stored } from '../made.ts'
import { useUrl } from '../use.ts'
import { draw, read, Refusal, stage, type Fleet, type Wire } from '../wire.ts'
import { lit, num, str, type Kind, type ViewProps, type Work } from './kind.ts'

/** The gateway's image models, best first. */
export const MODELS = [
  { id: 'google/gemini-3.1-flash-image', label: 'Nano Banana 2' },
  { id: 'google/gemini-3-pro-image', label: 'Nano Banana Pro' },
  { id: 'openai/gpt-5.4-image-2', label: 'GPT Image 2' },
  { id: 'openai/gpt-5-image-mini', label: 'GPT Image Mini' },
  { id: 'google/gemini-2.5-flash-image', label: 'Nano Banana' },
] as const

export const ZEN = 'zen-image'

export const ASPECTS = ['1:1', '4:5', '3:4', '2:3', '9:16', '16:9', '3:2', '4:3', '21:9'] as const

/** Pixel sizes for the GPU lane, on its 64-pixel grid. */
const PIXELS: Record<string, [number, number]> = {
  '1:1': [1024, 1024],
  '4:5': [896, 1120],
  '3:4': [896, 1152],
  '2:3': [832, 1248],
  '9:16': [768, 1344],
  '16:9': [1344, 768],
  '3:2': [1248, 832],
  '4:3': [1152, 896],
  '21:9': [1536, 640],
}

const ext = (mime: string) => (mime === 'image/jpeg' ? 'jpg' : mime === 'image/webp' ? 'webp' : 'png')

/** Which lane a request takes: the one asked for, else Zen Image on a GPU that can run it, else the gateway. */
export function lane(model: string, fleet: Fleet | null): string {
  if (model === ZEN || MODELS.some((m) => m.id === model)) return model
  return lit(fleet, 'image.zen') ? ZEN : MODELS[0].id
}

/** Which lane an edit takes: the one asked for, else Qwen Edit on a GPU that can run it, else the gateway. */
export function editing(model: string, fleet: Fleet | null): string {
  if (MODELS.some((m) => m.id === model)) return model
  if (lit(fleet, 'edit.qwen')) return ZEN
  if (model === ZEN) throw new Error('Editing on your GPU needs one that runs Qwen Edit; connect it with hanzo link, or edit with a gateway model.')
  return MODELS[0].id
}

async function size(blob: Blob): Promise<{ width?: number; height?: number }> {
  try {
    const bmp = await createImageBitmap(blob)
    const out = { width: bmp.width, height: bmp.height }
    bmp.close()
    return out
  } catch {
    return {}
  }
}

/*
 * Whether the gateway's image lane is answering, as the last call found it: a lane
 * that refused at the source says why, in the composer, until a call succeeds.
 */
let down: { model: string; reason: string } | null = null
const watchers = new Set<() => void>()
const note = (next: typeof down) => {
  down = next
  watchers.forEach((w) => w())
}

/** Why the gateway's image models are not answering now, or null. */
export function useImageLane(): { model: string; reason: string } | null {
  return useSyncExternalStore(
    (l) => {
      watchers.add(l)
      return () => watchers.delete(l)
    },
    () => down,
    () => down,
  )
}

/** Make pictures from words (and, for an edit, the pictures they start from), and keep them. */
export async function picture(w: Work, o: { prompt: string; aspect: string; model: string; from?: Stored[]; count?: number }): Promise<{ files: Stored[]; by: string }> {
  const use = o.from?.length ? editing(o.model, w.fleet) : lane(o.model, w.fleet)
  const files: Stored[] = []
  if (use === ZEN && o.from?.length) {
    for (const f of o.from) {
      w.doing('Sending the picture to your GPU')
      const upload = await stage(w.wire, await read(w.wire, f.key), f.name)
      const got = await render(w.wire, 'fix', { upload, instruction: o.prompt.slice(0, 500) }, w.doing, w.signal)
      for (const g of got) files.push(await w.keep(`edit-${files.length + 1}.png`, g.blob, await size(g.blob)))
    }
    return { files, by: 'Qwen Edit (your GPU)' }
  }
  if (use === ZEN) {
    const [width, height] = PIXELS[o.aspect] ?? PIXELS['1:1']!
    for (let i = 0; i < (o.count ?? 1); i++) {
      const got = await render(w.wire, 'image', { prompt: o.prompt, width, height }, w.doing, w.signal)
      for (const g of got) files.push(await w.keep(`image-${files.length + 1}.png`, g.blob, await size(g.blob)))
    }
    return { files, by: 'Zen Image (your GPU)' }
  }
  const from = await Promise.all((o.from ?? []).map((f) => w.picture(f)))
  const ask = from.length ? o.prompt : `${o.prompt}\n\nAspect ratio ${o.aspect}.`
  for (let i = 0; i < (o.count ?? 1); i++) {
    w.doing(from.length ? 'Editing the picture' : `Drawing${(o.count ?? 1) > 1 ? ` ${i + 1} of ${o.count}` : ''}`)
    let got
    try {
      got = await draw(w.wire, use, ask, { from, aspect: o.aspect, signal: w.signal })
    } catch (e) {
      if (e instanceof Refusal && (e.status >= 500 || e.status === 402)) note({ model: use, reason: e.message })
      throw e
    }
    note(null)
    for (const g of got) files.push(await w.keep(`image-${files.length + 1}.${ext(g.mime)}`, g.blob, await size(g.blob)))
  }
  return { files, by: MODELS.find((m) => m.id === use)?.label ?? use }
}

export const image: Kind = {
  id: 'image',
  label: 'Image',
  blurb: 'Pictures from words, and edits of a picture in words.',
  icon: Picture,
  tool: {
    name: 'make_image',
    description:
      'Make a new image from a description: a photo, an illustration, a product shot, a concept. For changing an image already in a tab, call revise instead. For an image with words laid on it (a poster, a flyer, an ad), call make_poster or make_ad.',
    parameters: {
      type: 'object',
      properties: {
        prompt: { type: 'string', description: 'A full visual description: subject, setting, light, lens, style, mood.' },
        title: { type: 'string', description: 'A short name for the tab, two to four words.' },
        aspect: { type: 'string', enum: ASPECTS },
        count: { type: 'integer', minimum: 1, maximum: 4, description: 'How many variations, 1 unless asked.' },
        from_tab: { type: 'integer', description: 'A tab whose picture this one starts from (a variation or a restyle).' },
      },
      required: ['prompt', 'title'],
    },
  },
  needs: () => null,
  make: async (w, args) => {
    const prompt = str(args.prompt)
    const aspect = (ASPECTS as readonly string[]).includes(str(args.aspect)) ? str(args.aspect) : '1:1'
    const model = str(args.model)
    const base = args.from_tab ? w.find(num(args.from_tab, 0)) : null
    const from = base ? current(base).files.filter((f) => f.mime.startsWith('image/')).slice(0, 1) : []
    const { files, by } = await picture(w, { prompt, aspect, model, from, count: num(args.count, 1, 1, 4) })
    return { title: str(args.title, prompt.slice(0, 40)), draft: { spec: { prompt, aspect, model }, files, note: prompt, by } }
  },
  revise: async (w, made, change) => {
    const now = current(made)
    const spec = { ...now.spec, ...(change.patch ?? {}) }
    const instruction = str(change.instruction) || str(spec.prompt)
    const from = now.files.filter((f) => f.mime.startsWith('image/')).slice(0, 1)
    // A new prompt typed in place redraws from words; an instruction edits the picture.
    const words = change.patch && typeof change.patch.prompt === 'string' && !change.instruction
    const model = str(spec.model)
    const { files, by } = await picture(w, {
      prompt: words ? str(spec.prompt) : instruction,
      aspect: str(spec.aspect, '1:1'),
      model: lane(model, w.fleet) === ZEN && !words ? MODELS[0].id : model,
      from: words ? [] : from,
    })
    return { spec: words ? spec : { ...spec, edit: instruction }, files, note: instruction, by }
  },
  View: Pictures,
  exports: [
    {
      label: 'Image file',
      run: async (w: Wire, m: Made, v) => {
        const f = v.files[0]!
        return { name: filename(m.title, f.name.split('.').pop() ?? 'png'), blob: await read(w, f.key) }
      },
    },
  ],
}

function Pictures({ made, version, wire, edit, busy }: ViewProps) {
  const pics = version.files.filter((f) => f.mime.startsWith('image/'))
  const [words, setWords] = useState(String(version.spec.prompt ?? ''))
  return (
    <YStack flex={1} minH={0}>
      <YStack flex={1} minH={0} items="center" justify="center" p="$3" gap="$3" overflow="scroll">
        {pics.length > 1 ? (
          <XStack flexWrap="wrap" gap="$3" justify="center">
            {pics.map((f) => (
              <Shot key={f.key} wire={wire} file={f} alt={made.title} max="45%" />
            ))}
          </XStack>
        ) : pics[0] ? (
          <Shot wire={wire} file={pics[0]} alt={made.title} max="100%" />
        ) : (
          <SizableText color="$soft">No picture in this version.</SizableText>
        )}
      </YStack>
      <XStack gap="$2" p="$3" borderTopWidth={1} borderColor="$borderColor" items="center">
        <input
          value={words}
          onChange={(e) => setWords(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && words.trim()) edit({ prompt: words.trim() }, `Redrawn: ${words.trim()}`)
          }}
          aria-label="The words this picture is drawn from"
          style={{ flex: 1, minWidth: 0, height: 32, padding: '0 10px', borderRadius: 8, border: '1px solid var(--borderColor)', background: 'transparent', color: 'inherit', font: 'inherit' }}
        />
        <XStack
          render="button"
          aria-label="Redraw from these words"
          onPress={() => words.trim() && edit({ prompt: words.trim() }, `Redrawn: ${words.trim()}`)}
          disabled={busy}
          px="$3"
          height={32}
          items="center"
          rounded="$3"
          borderWidth={1}
          borderColor="$borderColor"
          cursor="pointer"
          hoverStyle={{ bg: '$hover' }}
        >
          <SizableText size="$2">{busy ? 'Drawing…' : 'Redraw'}</SizableText>
        </XStack>
      </XStack>
    </YStack>
  )
}

export function Shot({ wire, file, alt, max }: { wire: Wire; file: Stored; alt: string; max: string }) {
  const { src, error } = useUrl(wire, file.key)
  if (error) return <SizableText color="$soft">{error}</SizableText>
  return src ? (
    <img src={src} alt={alt} width={file.width} height={file.height} style={{ maxWidth: max, maxHeight: '100%', height: 'auto', objectFit: 'contain', borderRadius: 12 }} />
  ) : (
    <YStack width={240} height={240} rounded="$4" bg="$hover" />
  )
}
