/**
 * Every kind Studio makes, in the order the home and the pills list them.
 */
import type { KindId } from '../made.ts'
import { ad } from './ad.tsx'
import { document_, slides } from './document.tsx'
import { image } from './image.tsx'
import type { Kind } from './kind.ts'
import { mesh } from './mesh/index.tsx'
import { poster } from './poster/index.tsx'
import { music, voice } from './sound.tsx'
import { type } from './type/index.tsx'
import { video } from './video.tsx'

export const ALL: readonly Kind[] = [image, poster, document_, slides, type, mesh, video, ad, voice, music]

export const kind = (id: KindId): Kind => ALL.find((k) => k.id === id) ?? image

/** The kind a tool name calls. */
export const called = (tool: string): Kind | null => ALL.find((k) => k.tool.name === tool) ?? null
