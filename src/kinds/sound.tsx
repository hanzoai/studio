/**
 * Sound: a voice reading words (the platform's speech, api.hanzo.ai
 * /v1/audio/speech, billed to the org), and music (ACE-Step 1.5 on the org's
 * GPU, offered while a GPU advertises `music.ace`).
 */
import { SizableText, YStack } from '@hanzo/gui'
import { AudioLines, Music } from '@hanzogui/lucide-icons-2'

import { render } from '../gpu.ts'
import { current, filename } from '../made.ts'
import { useUrl } from '../use.ts'
import { read, speak } from '../wire.ts'
import { lit, num, str, type Kind, type ViewProps } from './kind.ts'

/** The platform's voice and the voices it reads in. */
export const VOICE = 'zen-voice-mini'
export const VOICES = ['alloy', 'ash', 'coral', 'echo', 'fable', 'nova', 'onyx', 'sage', 'shimmer'] as const

export const voice: Kind = {
  id: 'voice',
  label: 'Voice',
  blurb: 'Words read aloud: voiceovers, narration, announcements.',
  icon: AudioLines,
  tool: {
    name: 'make_voice',
    description: 'Read words aloud as speech: a voiceover, narration, an announcement, a podcast intro. Write the exact words to be spoken.',
    parameters: {
      type: 'object',
      properties: {
        title: { type: 'string', description: 'A short name for the tab.' },
        script: { type: 'string', description: 'The exact words to speak, as they should be read.' },
        voice: { type: 'string', enum: VOICES },
      },
      required: ['title', 'script'],
    },
  },
  needs: () => null,
  make: async (w, args) => {
    const script = str(args.script)
    if (!script) throw new Error('Nothing to read.')
    const v = (VOICES as readonly string[]).includes(str(args.voice)) ? str(args.voice) : 'nova'
    w.doing('Reading it aloud')
    const file = await w.keep('voice.mp3', await speak(w.wire, script.slice(0, 4000), v, VOICE, w.signal))
    return { title: str(args.title, script.slice(0, 32)), draft: { spec: { script, voice: v }, files: [file], note: script.slice(0, 160), by: VOICE } }
  },
  revise: async (w, made, change) => {
    const now = current(made).spec as { script: string; voice: string }
    const next = { ...now, ...(change.patch ?? {}) } as { script: string; voice: string }
    if (change.instruction && !change.patch) next.script = change.instruction
    const file = await w.keep('voice.mp3', await speak(w.wire, next.script.slice(0, 4000), next.voice, VOICE, w.signal))
    return { spec: next, files: [file], note: change.instruction ?? 'Read again', by: VOICE }
  },
  View: Listen,
  exports: [{ label: 'MP3', run: async (w, m, v) => ({ name: filename(m.title, 'mp3'), blob: await read(w, v.files[0]!.key) }) }],
}

export const music: Kind = {
  id: 'music',
  label: 'Music',
  blurb: 'Songs and beds from a description, with lyrics or without.',
  icon: Music,
  tool: {
    name: 'make_music',
    description: 'Make music: a song, a jingle, a background bed. Describe the genre, mood, tempo and instruments; lyrics are optional.',
    parameters: {
      type: 'object',
      properties: {
        title: { type: 'string', description: 'A short name for the tab.' },
        prompt: { type: 'string', description: 'Genre, mood, tempo, instruments.' },
        lyrics: { type: 'string', description: 'Lyrics with [verse] and [chorus] marks, or empty for instrumental.' },
        seconds: { type: 'integer', minimum: 10, maximum: 240 },
      },
      required: ['title', 'prompt'],
    },
  },
  needs: (a) => (lit(a.fleet, 'music.ace') ? null : 'Music runs on a GPU. Connect one with `hanzo link`, and it appears here when the GPU can run it.'),
  make: async (w, args) => {
    const why = music.needs(w)
    if (why) throw new Error(why)
    const body = { prompt: str(args.prompt), lyrics: str(args.lyrics), seconds: num(args.seconds, 60, 10, 240) }
    const got = await render(w.wire, 'music', body, w.doing, w.signal)
    const g = got[0]!
    const ext = g.path.split('.').pop() || 'mp3'
    const file = await w.keep(`music.${ext}`, g.blob)
    return { title: str(args.title, body.prompt.slice(0, 32)), draft: { spec: body, files: [file], note: body.prompt, by: 'ACE-Step 1.5 (your GPU)' } }
  },
  revise: async (w, made, change) => {
    const now = current(made).spec as { prompt: string; lyrics: string; seconds: number }
    const { draft } = await music.make(w, { title: made.title, ...now, ...(change.patch ?? {}), prompt: change.instruction ? `${now.prompt}. ${change.instruction}` : now.prompt })
    return draft
  },
  View: Listen,
  exports: [{ label: 'Audio file', run: async (w, m, v) => ({ name: filename(m.title, v.files[0]!.name.split('.').pop() ?? 'mp3'), blob: await read(w, v.files[0]!.key) }) }],
}

function Listen({ version, wire, made }: ViewProps) {
  const f = version.files[0]
  const { src, error } = useUrl(wire, f?.key)
  const words = String((version.spec as { script?: string; lyrics?: string }).script ?? (version.spec as { lyrics?: string }).lyrics ?? '')
  return (
    <YStack flex={1} minH={0} p="$4" gap="$4" items="center" justify="center">
      {error ? <SizableText color="$soft">{error}</SizableText> : src ? <audio controls src={src} aria-label={made.title} style={{ width: '100%', maxWidth: 520 }} /> : null}
      {words ? (
        <SizableText size="$3" color="$soft" maxW={520} whiteSpace="pre-wrap">
          {words}
        </SizableText>
      ) : null}
    </YStack>
  )
}
