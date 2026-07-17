import { useEffect, useRef, useState } from 'react'
import {
  chat, getSession, getLibrary, getRenderQueue, gpuOnline, thumbURL,
  type ChatMessage, type Session, type Asset, type RenderJob,
} from './api'

// Studio-chat: the specialized chat surface. You say what to create; the cloud
// /v1/agent "create" preset drives studio's render tools (fix/compose) and the
// outputs land in the library strip. Increment 1 uses a minimal own-UI; the next
// increment swaps the transcript/composer for @hanzo/chat's <Chat> on @hanzo/gui.

export function App() {
  const [session, setSession] = useState<Session>({})
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [conversationId, setConversationId] = useState<string>()
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [assets, setAssets] = useState<Asset[]>([])
  const [jobs, setJobs] = useState<RenderJob[]>([])
  const [gpu, setGpu] = useState(false)
  const scroller = useRef<HTMLDivElement>(null)

  useEffect(() => { getSession().then(setSession) }, [])

  // Poll the library + in-flight queue so dispatched renders appear as they land.
  useEffect(() => {
    let alive = true
    const tick = async () => {
      if (!alive) return
      const [lib, q, on] = await Promise.all([getLibrary(), getRenderQueue(), gpuOnline()])
      if (!alive) return
      setAssets(lib.assets.filter(a => a.status !== 'deleted').slice(0, 24))
      setJobs(q.jobs); setGpu(on)
    }
    tick()
    const t = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  useEffect(() => { scroller.current?.scrollTo(0, scroller.current.scrollHeight) }, [messages, busy])

  async function send() {
    const text = input.trim()
    if (!text || busy) return
    const next = [...messages, { role: 'user' as const, content: text }]
    setMessages(next); setInput(''); setBusy(true)
    try {
      const r = await chat(next, conversationId)
      setConversationId(r.conversationId)
      const note = r.actions.length
        ? `\n\n${r.actions.map(a => a.error ? `⚠︎ ${a.name}: ${a.error}` : `▸ ${a.name} dispatched`).join('\n')}`
        : ''
      setMessages([...next, { role: 'assistant', content: (r.reply || '…') + note }])
    } catch (e) {
      setMessages([...next, { role: 'assistant', content: `Couldn't reach the studio chat (${(e as Error).message}).` }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app">
      <header className="top">
        <div className="brand">Hanzo <b>Studio</b></div>
        <div className="spacer" />
        <div className={`gpu ${gpu ? 'on' : 'off'}`}>{gpu ? 'GPU online' : 'no GPU'}</div>
        <div className="who">{session.authenticated ? (session.org || session.user || 'signed in') : 'sign in'}</div>
      </header>

      <main className="body">
        <section className="chat">
          <div className="scroll" ref={scroller}>
            {messages.length === 0 && (
              <div className="hero">
                <h1>What should we create?</h1>
                <p>Describe an edit or a new shot — “make the Valentina back crotchless”,
                  “put design 13 on model 01”. I’ll run it on your GPU and it lands below.</p>
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}><div className="bubble">{m.content}</div></div>
            ))}
            {busy && <div className="msg assistant"><div className="bubble dim">working…</div></div>}
          </div>
          <div className="composer">
            <textarea
              value={input} placeholder="Describe what to create or change…"
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            />
            <button onClick={send} disabled={busy || !input.trim()}>Send</button>
          </div>
        </section>

        <aside className={`rail${assets.length || jobs.length ? ' active' : ''}`}>
          {jobs.length > 0 && (
            <div className="queue">
              <div className="rail-h">Rendering ({jobs.length})</div>
              {jobs.map(jb => <div key={jb.id} className="job">{jb.prefix || jb.id.slice(0, 8)}</div>)}
            </div>
          )}
          <div className="rail-h">Library</div>
          <div className="grid">
            {assets.map(a => (
              <a key={a.path} className="cell" href={thumbURL(a.path)} target="_blank" rel="noreferrer">
                <img loading="lazy" src={thumbURL(a.path)} alt="" />
              </a>
            ))}
            {assets.length === 0 && <div className="empty">Renders appear here.</div>}
          </div>
        </aside>
      </main>
    </div>
  )
}
