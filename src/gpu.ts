/**
 * A render on the org's GPUs, start to finish: dispatch through the engine's
 * one funnel, follow its work-log row until the engine's integrity gate says
 * done (or failed), and read back what landed.
 *
 * The engine's word is the only verdict: `done` is the row its gate closed, a
 * `failed` row carries the engine's reason, and a row still `queued` when the
 * wait runs out is said to be still rendering, never called finished.
 */
import { dispatch, landed, Refusal, worklog, type Row, type Wire } from './wire.ts'

/** How long a render is followed, ms: a cold model load plus a video. */
const WAIT = 20 * 60_000
const EVERY = 3_000

export type Lane = Parameters<typeof dispatch>[1]

export async function render(w: Wire, lane: Lane, body: object, doing: (line: string) => void, signal: AbortSignal): Promise<{ blob: Blob; path: string }[]> {
  doing('Sending to your GPU')
  const sent = await dispatch(w, lane, body, signal)
  const id = sent.prompt_id ?? sent.id
  if (!id) throw new Refusal(502, 'The engine took the render but named no job.')
  const until = Date.now() + WAIT
  let row: Row | undefined
  for (;;) {
    if (signal.aborted) throw new DOMException('aborted', 'AbortError')
    row = (await worklog(w, signal)).find((r) => r.prompt_id === id || r.id === id)
    const status = row?.status ?? 'queued'
    if (status === 'done') break
    if (status === 'failed' || status === 'cancelled') throw new Refusal(502, row?.error || `The render ${status}.`)
    if (Date.now() > until) throw new Refusal(504, 'Still rendering on your GPU; it will land in the library when it finishes.')
    doing(status === 'running' ? 'Rendering on your GPU' : 'Waiting for a GPU')
    await new Promise((r) => setTimeout(r, EVERY))
  }
  const paths = row?.outputs?.length ? row.outputs : row?.output ? [row.output] : []
  if (!paths.length) throw new Refusal(502, 'The render finished with no file.')
  doing('Reading the result')
  return Promise.all(paths.map(async (path) => ({ path, blob: await landed(w, path) })))
}
