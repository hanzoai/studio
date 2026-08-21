import { useEffect, useRef, useState } from 'react'
import { chat, getSession, gpuOnline, type ChatMessage, type Session } from './api'
import { Rail } from './rail'
import { useLayout, startDrag, clamp, DEFAULT, RAIL_MIN, RAIL_MAX } from './layout'

// Studio-chat: the specialized chat surface. You say what to create; the cloud
// /v1/agents/chat "create" preset drives studio's render tools (fix/compose) and the runs
// land in the queue/history rail on the right — Up next while they render, History
// once done. The main area is the conversation / current generation; the rail is the
// whole YouTube-watch-page-style queue + history + stacks + ratings surface.

const EXAMPLES = [
  'make the Valentina back crotchless',
  'put design 13 on model 01',
  'brighten the studio lighting',
  'compose design 4 and design 9 into one look',
]

export function App() {
  const [session, setSession] = useState<Session>({})
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [conversationId, setConversationId] = useState<string>()
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [gpu, setGpu] = useState(false)
  const [acct, setAcct] = useState(false)
  const scroller = useRef<HTMLDivElement>(null)
  const composer = useRef<HTMLTextAreaElement>(null)

  // Layout is keyed by org+user so each person's dividers/collapse/filter persist.
  const who = session.authenticated !== undefined ? `${session.org || 'default'}:${session.user || 'anon'}` : ''
  const { layout, patch, toggle, reset, beginDrag, endDrag } = useLayout(who)

  useEffect(() => { getSession().then(setSession) }, [])
  useEffect(() => {
    let alive = true
    const tick = () => gpuOnline().then(on => { if (alive) setGpu(on) })
    tick(); const t = setInterval(tick, 15000)
    return () => { alive = false; clearInterval(t) }
  }, [])
  useEffect(() => { scroller.current?.scrollTo(0, scroller.current.scrollHeight) }, [messages, busy])

  const ready = !!input.trim() && !busy

  async function send() {
    const text = input.trim()
    if (!text || busy) return
    const next = [...messages, { role: 'user' as const, content: text }]
    setMessages(next); setInput(''); setBusy(true)
    try {
      const r = await chat(next, conversationId)
      setConversationId(r.conversationId)
      const note = r.actions.length
        ? `\n\n${r.actions.map(a => a.error ? `⚠︎ ${a.name}: ${a.error}` : `▸ ${a.name} dispatched — see Up next →`).join('\n')}`
        : ''
      setMessages([...next, { role: 'assistant', content: (r.reply || '…') + note }])
    } catch (e) {
      setMessages([...next, { role: 'assistant', content: `Couldn't reach the studio chat (${(e as Error).message}).` }])
    } finally {
      setBusy(false)
    }
  }

  function insertRef(text: string) {
    setInput(v => (v.trim() ? `${v.trim()} ${text}` : text) + ' ')
    composer.current?.focus()
  }
  function useExample(ex: string) {
    setInput(ex); composer.current?.focus()
  }

  function onVSplit(e: React.PointerEvent) {
    beginDrag()
    const startW = layout.railW
    startDrag(e, 'x', d => patch({ railW: clamp(startW - d, RAIL_MIN, RAIL_MAX) }), endDrag)
  }

  const initials = (session.user || session.org || session.email || '?').replace(/[^a-zA-Z0-9]/g, '').slice(0, 2).toUpperCase() || '?'

  return (
    <div className="app">
      <header className="top">
        <div className="brand">Hanzo <b>Studio</b></div>
        <div className="spacer" />
        <div className={`gpu ${gpu ? 'on' : 'off'}`}>{gpu ? 'GPU online' : 'no GPU'}</div>
        <div className="acct-wrap">
          <button className="avatar" aria-label="account" aria-haspopup="menu"
                  title={session.email || session.user || 'account'} onClick={() => setAcct(a => !a)}>
            {initials}
          </button>
          {acct && (
            <div className="rc-menu acct-menu" role="menu" onMouseLeave={() => setAcct(false)}>
              <div className="acct-id">
                <div className="acct-name">{session.user || (session.authenticated ? 'signed in' : 'guest')}</div>
                {session.email && <div className="acct-sub">{session.email}</div>}
                {session.org && <div className="acct-sub">org · {session.org}</div>}
              </div>
              <a role="menuitem" href="https://console.hanzo.ai" target="_blank" rel="noreferrer">Console</a>
              {session.authenticated
                ? <a role="menuitem" href="/logout">Sign out</a>
                : <a role="menuitem" href={session.iam_url || '/login'}>Sign in with Hanzo</a>}
            </div>
          )}
        </div>
      </header>

      <main className="body">
        <section className="chat">
          <div className="scroll" ref={scroller}>
            {messages.length === 0 && (
              <div className="hero">
                <h1>What should we create?</h1>
                <p>Describe an edit or a new shot. Runs appear in <b>Up next</b> on the right as they render,
                  and finished results land in <b>History</b> — rate them to teach the next pass.</p>
                <div className="examples">
                  {EXAMPLES.map(ex => <button key={ex} className="ex-chip" onClick={() => useExample(ex)}>{ex}</button>)}
                </div>
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}><div className="bubble">{m.content}</div></div>
            ))}
            {busy && <div className="msg assistant"><div className="bubble dim">working…</div></div>}
          </div>
          <div className="composer">
            <textarea
              ref={composer} value={input} placeholder="Describe what to create or change…"
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            />
            <button className={`send${ready ? ' ready' : ''}`} onClick={send} disabled={busy}>Send</button>
          </div>
        </section>

        <div className="vsplit" onPointerDown={onVSplit} onDoubleClick={() => patch({ railW: DEFAULT.railW })}
             title="drag to resize · double-click to reset" role="separator" aria-orientation="vertical" />

        <aside className="rail" style={{ width: layout.railW }}>
          <Rail layout={layout} patch={patch} toggle={toggle} reset={reset}
                beginDrag={beginDrag} endDrag={endDrag} onUseRef={insertRef} />
        </aside>
      </main>
    </div>
  )
}
