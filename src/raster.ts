/**
 * An SVG drawn to pixels in the browser: the one rasterizer posters and ads
 * use. The SVG carries its own faces and pictures as data: URLs, because an SVG
 * drawn as an image may load nothing from outside itself.
 */
import { pdf } from './pdf.ts'

export async function pixels(svg: string, width: number, height: number, type: 'image/png' | 'image/jpeg' = 'image/png', quality = 0.92): Promise<Blob> {
  const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }))
  try {
    const img = new Image()
    img.decoding = 'async'
    img.src = url
    await img.decode()
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('This browser cannot draw a picture.')
    ctx.drawImage(img, 0, 0, width, height)
    return await new Promise<Blob>((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('The picture could not be written.'))), type, quality))
  } finally {
    URL.revokeObjectURL(url)
  }
}

/** One page of PDF at a physical size (points), the SVG drawn at `width`x`height` pixels. */
export async function page(svg: string, width: number, height: number, points: [number, number], title: string): Promise<Blob> {
  const jpeg = new Uint8Array(await (await pixels(svg, width, height, 'image/jpeg', 0.94)).arrayBuffer())
  return pdf([{ jpeg, width, height, pageWidth: points[0], pageHeight: points[1] }], title)
}

/** A stored picture as a data: URL. */
export async function data(blob: Blob): Promise<string> {
  const buf = new Uint8Array(await blob.arrayBuffer())
  let bin = ''
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
  return `data:${blob.type || 'image/png'};base64,${btoa(bin)}`
}

/** A picture as a JPEG data: URL no larger than `side` on its long edge: what a document carries inline. */
export async function shrink(blob: Blob, side = 1600, quality = 0.86): Promise<string> {
  const bmp = await createImageBitmap(blob)
  const k = Math.min(1, side / Math.max(bmp.width, bmp.height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.round(bmp.width * k)
  canvas.height = Math.round(bmp.height * k)
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('This browser cannot draw a picture.')
  ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height)
  bmp.close()
  return canvas.toDataURL('image/jpeg', quality)
}
