import { describe, expect, it } from 'vitest'

import { pdf } from './pdf.ts'
import { zip } from './zip.ts'

const text = async (b: Blob) => new TextDecoder('latin1').decode(new Uint8Array(await b.arrayBuffer()))

describe('the files Studio writes', () => {
  it('writes a PDF whose cross reference points at every object', async () => {
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9])
    const b = pdf([{ jpeg, width: 2, height: 3, pageWidth: 595.28, pageHeight: 841.89 }], 'A (test)')
    const s = await text(b)
    expect(s.startsWith('%PDF-1.4')).toBe(true)
    expect(s.trimEnd().endsWith('%%EOF')).toBe(true)
    expect(s).toContain('/MediaBox [0 0 595.28 841.89]')
    expect(s).toContain('/Title (A test)')
    const at = Number(/startxref\n(\d+)/.exec(s)![1])
    expect(s.slice(at, at + 4)).toBe('xref')
    const offsets = [...s.slice(at).matchAll(/^(\d{10}) 00000 n $/gm)].map((m) => Number(m[1]))
    offsets.forEach((o, i) => expect(s.slice(o).startsWith(`${i + 1} 0 obj`)).toBe(true))
  })
  it('writes a ZIP with the CRC every reader checks', async () => {
    const z = new Uint8Array(await (await zip([{ name: 'a.txt', blob: new Blob(['hello']) }])).arrayBuffer())
    const v = new DataView(z.buffer)
    expect(v.getUint32(0, true)).toBe(0x04034b50)
    expect(v.getUint32(14, true)).toBe(0x3610a686) // crc32("hello")
    expect(v.getUint32(z.length - 22, true)).toBe(0x06054b50)
  })
})
