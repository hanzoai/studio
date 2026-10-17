/**
 * Short video: a clip from words, or from a picture. Two lanes: a gateway video
 * model (the async /v1/videos API, billed to the org) when the catalog serves
 * one, and Wan 2.2 TI2V 5B on the org's GPU while a GPU advertises `video.wan`.
 * Long form (script, storyboard, shots, cut) is Films.
 */
import { SizableText, YStack } from '@hanzo/gui'
import { Clapperboard } from '@hanzogui/lucide-icons-2'

import { render } from '../gpu.ts'
import { current, filename } from '../made.ts'
import { useUrl } from '../use.ts'
import { film, read, stage } from '../wire.ts'
import { lit, maker, num, str, type Available, type Kind, type ViewProps } from './kind.ts'

const SIZES: Record<string, { gpu: [number, number]; size: string }> = {
  '16:9': { gpu: [832, 480], size: '1280x720' },
  '9:16': { gpu: [480, 832], size: '720x1280' },
  '1:1': { gpu: [640, 640], size: '960x960' },
}

/** Frames at the GPU lane's 24 fps for a length in seconds. */
const frames = (s: number) => Math.round(s * 24) + 1

const lane = (a: Available): 'gateway' | 'gpu' | null => (maker(a, 'video') ? 'gateway' : lit(a.fleet, 'video.wan') ? 'gpu' : null)

export const video: Kind = {
  id: 'video',
  label: 'Video',
  blurb: 'Short clips for ads and social, from words or a picture.',
  icon: Clapperboard,
  tool: {
    name: 'make_video',
    description: 'Make a short video clip (2 to 10 seconds): an ad shot, a social clip, a product in motion. From a description, or starting from a picture in a tab. For a whole film with a script and many shots, say so and the person opens Films.',
    parameters: {
      type: 'object',
      properties: {
        title: { type: 'string', description: 'A short name for the tab.' },
        prompt: { type: 'string', description: 'What happens, the camera move, the light, the style.' },
        aspect: { type: 'string', enum: Object.keys(SIZES) },
        seconds: { type: 'integer', minimum: 2, maximum: 10 },
        image_tab: { type: 'integer', description: 'A tab whose picture the clip starts from.' },
      },
      required: ['title', 'prompt'],
    },
  },
  needs: (a) => (lane(a) ? null : 'Video needs a video model or a GPU. Connect a GPU with `hanzo link`, and it appears here when the GPU can run it.'),
  make: async (w, args) => {
    const use = lane(w)
    if (!use) throw new Error(video.needs(w)!)
    const prompt = str(args.prompt)
    const aspect = SIZES[str(args.aspect)] ? str(args.aspect) : '16:9'
    const seconds = num(args.seconds, 5, 2, 10)
    const tab = num(args.image_tab, 0) ? w.find(num(args.image_tab, 0)) : null
    const from = tab ? current(tab).files.find((f) => f.mime.startsWith('image/')) : undefined
    let blob: Blob
    let by: string
    if (use === 'gateway') {
      const m = maker(w, 'video')!
      blob = await film(w.wire, m.id, prompt, { size: SIZES[aspect]!.size, seconds, doing: w.doing, signal: w.signal })
      by = m.name
    } else {
      const [width, height] = SIZES[aspect]!.gpu
      const upload = from ? await stage(w.wire, await read(w.wire, from.key), 'start.png') : undefined
      const got = await render(w.wire, 'video', { prompt, width, height, length: frames(Math.min(seconds, 5)), upload }, w.doing, w.signal)
      blob = got[0]!.blob
      by = 'Wan 2.2 (your GPU)'
    }
    const file = await w.keep('clip.mp4', new Blob([blob], { type: 'video/mp4' }))
    return { title: str(args.title, prompt.slice(0, 32)), draft: { spec: { prompt, aspect, seconds, from: from?.key ?? '' }, files: [file], note: prompt, by } }
  },
  revise: async (w, made, change) => {
    const now = current(made).spec as { prompt: string; aspect: string; seconds: number }
    const { draft } = await video.make(w, { title: made.title, ...now, ...(change.patch ?? {}), prompt: change.instruction ? `${now.prompt}. ${change.instruction}` : now.prompt })
    return draft
  },
  View: Clip,
  exports: [{ label: 'MP4', run: async (w, m, v) => ({ name: filename(m.title, 'mp4'), blob: await read(w, v.files[0]!.key) }) }],
}

function Clip({ version, wire, made }: ViewProps) {
  const { src, error } = useUrl(wire, version.files[0]?.key)
  return (
    <YStack flex={1} minH={0} items="center" justify="center" p="$3">
      {error ? <SizableText color="$soft">{error}</SizableText> : src ? <video controls loop playsInline src={src} aria-label={made.title} style={{ maxWidth: '100%', maxHeight: '100%', borderRadius: 12 }} /> : null}
    </YStack>
  )
}
