/**
 * A PDF of pictures, one per page, each page the physical size asked for.
 * The pictures are JPEG, which PDF carries as they are (DCTDecode), so this is
 * the whole writer: a catalog, the pages, one image each, and the cross
 * reference table. No fonts, no text: what is drawn is the picture.
 */

export interface Sheet {
  jpeg: Uint8Array
  /** The picture's pixels. */
  width: number
  height: number
  /** The page, in points (1/72 inch). */
  pageWidth: number
  pageHeight: number
}

const enc = new TextEncoder()

export function pdf(sheets: readonly Sheet[], title: string): Blob {
  const parts: Uint8Array[] = []
  const offsets: number[] = []
  let at = 0
  const push = (b: Uint8Array) => {
    parts.push(b)
    at += b.length
  }
  const text = (s: string) => push(enc.encode(s))
  const object = (n: number, body: () => void) => {
    offsets[n] = at
    text(`${n} 0 obj\n`)
    body()
    text('\nendobj\n')
  }
  text('%PDF-1.4\n%\xE2\xE3\xCF\xD3\n')
  const n = sheets.length
  // 1 catalog, 2 pages, 3 info; per sheet: page, content, image.
  const page = (i: number) => 4 + i * 3
  const kids = sheets.map((_, i) => `${page(i)} 0 R`).join(' ')
  const safe = title.replace(/[()\\]/g, '').slice(0, 120)
  object(1, () => text('<< /Type /Catalog /Pages 2 0 R >>'))
  object(2, () => text(`<< /Type /Pages /Kids [${kids}] /Count ${n} >>`))
  object(3, () => text(`<< /Title (${safe}) /Producer (Hanzo Studio) >>`))
  sheets.forEach((s, i) => {
    const p = page(i)
    const w = s.pageWidth.toFixed(2)
    const h = s.pageHeight.toFixed(2)
    object(p, () => text(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${w} ${h}] /Resources << /XObject << /Im0 ${p + 2} 0 R >> >> /Contents ${p + 1} 0 R >>`))
    const draw = `q ${w} 0 0 ${h} 0 0 cm /Im0 Do Q`
    object(p + 1, () => text(`<< /Length ${draw.length} >>\nstream\n${draw}\nendstream`))
    object(p + 2, () => {
      text(`<< /Type /XObject /Subtype /Image /Width ${s.width} /Height ${s.height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${s.jpeg.length} >>\nstream\n`)
      push(s.jpeg)
      text('\nendstream')
    })
  })
  const xref = at
  const count = 4 + n * 3
  let table = `xref\n0 ${count}\n0000000000 65535 f \n`
  for (let i = 1; i < count; i++) table += `${String(offsets[i] ?? 0).padStart(10, '0')} 00000 n \n`
  text(table)
  text(`trailer\n<< /Size ${count} /Root 1 0 R /Info 3 0 R >>\nstartxref\n${xref}\n%%EOF\n`)
  return new Blob(parts as BlobPart[], { type: 'application/pdf' })
}
