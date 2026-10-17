/**
 * 3D: a mesh from a picture, or from words (the image lane draws the picture
 * first). It runs on the org's GPU, through the engine's 3D route, and only
 * while a GPU advertises the `mesh` lane; until then the kind says it needs a
 * GPU and the home does not offer it. The tab is a glTF viewer: orbit, zoom,
 * and the .glb to take away.
 */
import { SizableText, YStack } from '@hanzo/gui'
import { Box } from '@hanzogui/lucide-icons-2'
import { lazy, Suspense } from 'react'

import { render } from '../../gpu.ts'
import { current, filename } from '../../made.ts'
import { read, stage } from '../../wire.ts'
import { picture } from '../image.tsx'
import { lit, num, str, type Kind, type ViewProps } from '../kind.ts'

const Viewer = lazy(() => import('./viewer.tsx'))

export const mesh: Kind = {
  id: 'mesh',
  label: '3D',
  blurb: 'A 3D model from a picture or from words, as glTF.',
  icon: Box,
  tool: {
    name: 'make_3d',
    description: 'Make a 3D model (a mesh, glTF/.glb) of one object: from a picture in a tab, or from a description (a picture of it is drawn first).',
    parameters: {
      type: 'object',
      properties: {
        title: { type: 'string', description: 'A short name for the tab.' },
        prompt: { type: 'string', description: 'The object, alone on a plain background, three-quarter view.' },
        image_tab: { type: 'integer', description: 'A tab whose picture to model.' },
      },
      required: ['title'],
    },
  },
  needs: (a) => (lit(a.fleet, 'mesh') ? null : '3D runs on a GPU. Connect one with `hanzo link`, and it appears here when the GPU can run it.'),
  make: async (w, args) => {
    const why = mesh.needs(w)
    if (why) throw new Error(why)
    const tab = num(args.image_tab, 0) ? w.find(num(args.image_tab, 0)) : null
    let from = tab ? current(tab).files.find((f) => f.mime.startsWith('image/')) : undefined
    const prompt = str(args.prompt)
    if (!from) {
      if (!prompt) throw new Error('Say what to model, or name a tab with a picture.')
      const got = await picture(w, { prompt: `${prompt}, a single object centred on a plain light background, three-quarter view, soft even light`, aspect: '1:1', model: '' })
      from = got.files[0]
    }
    if (!from) throw new Error('No picture to model.')
    const upload = await stage(w.wire, await read(w.wire, from.key), 'reference.png')
    const got = await render(w.wire, 'model3d', { upload, prompt }, w.doing, w.signal)
    const glb = got.find((g) => g.path.endsWith('.glb')) ?? got[0]!
    const file = await w.keep('model.glb', new Blob([glb.blob], { type: 'model/gltf-binary' }))
    return { title: str(args.title, prompt.slice(0, 40) || 'Model'), draft: { spec: { prompt, picture: from.key }, files: [file, from], note: prompt || 'From a picture', by: 'your GPU' } }
  },
  revise: async (w, made, change) => {
    const prompt = str(change.instruction) || str(current(made).spec.prompt)
    const { draft } = await mesh.make(w, { title: made.title, prompt })
    return draft
  },
  View: Model,
  exports: [
    {
      label: 'glTF binary (.glb)',
      run: async (w, m, v) => ({ name: filename(m.title, 'glb'), blob: await read(w, v.files.find((f) => f.name.endsWith('.glb'))!.key) }),
    },
  ],
}

function Model({ version, wire, made }: ViewProps) {
  const glb = version.files.find((f) => f.name.endsWith('.glb'))
  if (!glb) return <SizableText color="$soft">No model in this version.</SizableText>
  return (
    <YStack flex={1} minH={0}>
      <Suspense fallback={<SizableText color="$soft">Opening the viewer…</SizableText>}>
        <Viewer wire={wire} file={glb} title={made.title} />
      </Suspense>
    </YStack>
  )
}
