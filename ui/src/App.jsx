import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useEventStream } from './useEventStream.js'
import ChatPanel from './ChatPanel.jsx'
import SystemView from './SystemView.jsx'
import TeamSystemView from './TeamSystemView.jsx'
import AdaptiveSystemView from './AdaptiveSystemView.jsx'
import AuditLog from './AuditLog.jsx'

// The UI holds no access-control logic. It only asserts who the user is (by
// starting a session for them); the broker decides what that user may do.
export default function App() {
  const [presets, setPresets] = useState({ users: [], objectives: [], team_agents: [], team_objectives: [], adaptive_objectives: [] })
  const [user, setUser] = useState(null)
  const [mode, setMode] = useState('adaptive')
  const [sessionId, setSessionId] = useState(null)
  const [sessionEvents, setSessionEvents] = useState([])
  const [userMessages, setUserMessages] = useState([])
  const [busy, setBusy] = useState(false)
  const [auditTick, setAuditTick] = useState(0)
  const [health, setHealth] = useState(null)
  const sessionRef = useRef(null)

  useEffect(() => {
    fetch('/api/presets').then((r) => r.json()).then((p) => {
      setPresets(p)
      startSession(p.users[0].id, 'adaptive')
    })
    fetch('/api/health').then((r) => r.json()).then(setHealth)
  }, [])

  // Switching user or mode starts a fresh session (and conversation).
  const startSession = async (id, m) => {
    const r = await fetch('/api/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user: id, mode: m }),
    })
    const s = await r.json()
    sessionRef.current = s.session_id
    setUser(id)
    setMode(s.mode)
    setSessionId(s.session_id)
    setSessionEvents([])
    setUserMessages([])
    setBusy(false)
  }

  const onEvent = useCallback((event) => {
    if (event.type === 'decision' || event.type === 'delegation') setAuditTick((t) => t + 1) // audit log re-reads the broker's log
    if (event.session_id !== sessionRef.current) return
    setSessionEvents((evs) => [...evs, { ...event, seq: evs.length }])
    if (event.type === 'agent_message') setBusy(false)
  }, [])

  const connected = useEventStream(onEvent)

  // Safety net: if the socket dropped while the agent was working, the reply
  // could be missed. While busy, check the session; if the agent has finished,
  // show its last reply (the same agent_message event) and clear the spinner.
  useEffect(() => {
    if (!busy || !sessionId) return
    const t = setInterval(async () => {
      const r = await fetch(`/api/session/${sessionId}`).then((x) => x.json()).catch(() => null)
      if (!r || r.busy) return
      setSessionEvents((evs) => {
        const reply = r.last_reply
        if (!reply || evs.some((e) => e.type === 'agent_message' && e.ts === reply.ts)) return evs
        return [...evs, { ...reply, seq: evs.length }]
      })
      setBusy(false)
    }, 4000)
    return () => clearInterval(t)
  }, [busy, sessionId])

  const send = async (text) => {
    if (!text.trim() || busy) return
    setBusy(true)
    setUserMessages((m) => [...m, { text, afterSeq: sessionEvents.length }])
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    })
    if (!r.ok) {
      setBusy(false)
      const detail = await r.json().then((b) => b.detail).catch(() => r.statusText)
      setSessionEvents((evs) => [...evs, { type: 'agent_message', ts: new Date().toISOString(), session_id: sessionId,
        source: 'ui', payload: { text: `Couldn't send that: ${detail}`, error: true }, seq: evs.length }])
    }
  }

  const current = presets.users.find((u) => u.id === user)

  // In Auto mode the view follows the work: an escalation switches to the team
  // view; the assistant's next own plan or request switches back.
  let view = mode, viewFrom = 0
  if (mode === 'single') {
    for (const e of sessionEvents) {
      if (e.type === 'agent_escalate') { view = 'team'; viewFrom = e.seq }
      else if (view === 'team' && ['agent_plan', 'request_sent'].includes(e.type) && e.payload.agent_id === 'assistant-agent') { view = 'single'; viewFrom = e.seq }
    }
  }
  const viewEvents = viewFrom ? sessionEvents.slice(viewFrom) : sessionEvents

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">◆</span>
          <div>
            <div className="brand-title">Brokered Agent Access</div>
            <div className="brand-sub">Agent → Broker → Database · every event shown here is real</div>
          </div>
        </div>
        <div className="mode-switch" role="radiogroup" aria-label="Agent mode">
          {[['adaptive', 'Adaptive', 'The assistant does simple tasks itself and adds specialists only when needed, in parallel'],
            ['single', 'Auto', 'One assistant; complex tasks are handed to the fixed agent team'],
            ['team', 'Agent team', 'Every task goes to the orchestrator and its specialists']].map(([m, label, hint]) => (
            <button key={m} role="radio" aria-checked={mode === m} data-mode={m} title={hint}
                    className={`mode-btn ${mode === m ? 'active' : ''}`}
                    onClick={() => mode !== m && startSession(user, m)}>
              {label}
            </button>
          ))}
        </div>
        <div className="user-switcher" role="radiogroup" aria-label="Acting user">
          <span className="switcher-label">Acting as</span>
          {presets.users.map((u) => (
            <button
              key={u.id}
              role="radio"
              aria-checked={u.id === user}
              data-user={u.id}
              className={`user-btn ${u.id === user ? 'active' : ''}`}
              onClick={() => u.id !== user && startSession(u.id, mode)}
            >
              <span className="user-btn-text">
                <span className="user-name">{u.name}</span>
                <span className="user-role">{u.role}</span>
              </span>
            </button>
          ))}
        </div>
        <div className="status">
          <span className={`dot ${connected ? 'ok' : 'bad'}`} /> {connected ? 'live' : 'reconnecting'}
          {health && <span className="model">{health.llm_provider ? `${health.llm_provider} · ${health.llm_model}` : 'no LLM key'}</span>}
        </div>
      </header>

      <main className="grid">
        <ChatPanel
          user={current}
          events={sessionEvents}
          userMessages={userMessages}
          objectives={{ team: presets.team_objectives, adaptive: presets.adaptive_objectives }[mode] || presets.objectives}
          agents={presets.team_agents}
          mode={mode}
          busy={busy}
          onSend={send}
        />
        <div className="right">
          {mode === 'adaptive'
            ? <AdaptiveSystemView key={sessionId} user={current} events={sessionEvents} agents={presets.team_agents} />
            : view === 'team'
            ? <TeamSystemView key={`${sessionId}-${viewFrom}`} user={current} events={viewEvents} agents={presets.team_agents} />
            : <SystemView key={`${sessionId}-${viewFrom}`} user={current} events={viewEvents} />}
          <AuditLog tick={auditTick} sessionId={sessionId} />
        </div>
      </main>
    </div>
  )
}
