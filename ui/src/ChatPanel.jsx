import React, { useEffect, useMemo, useRef, useState } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

const md = (text) => ({ __html: DOMPurify.sanitize(marked.parse(text || '')) })

export function describeRequest(p) {
  const filter = p.filter && Object.keys(p.filter).length
    ? ' where ' + Object.entries(p.filter).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' and ')
    : ''
  return `${p.action} ${p.table}(${(p.columns || []).join(', ')})${filter}`
}

// Shows how long the agent has been on this turn and what it last did, so a
// slow local model never looks like silence.
function Working({ events }) {
  const [start] = useState(Date.now())
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  const last = [...events].reverse().find((e) => e.source !== 'ui')
  const label = {
    agent_plan: 'working through the plan',
    agent_thinking: 'thinking',
    request_sent: 'waiting for the broker',
    broker_check: 'broker is checking the request',
    decision: 'reading the broker\'s answer',
    result_returned: 'thinking about the result',
    agent_escalate: 'handing this to the agent team',
    agent_dispatch: 'sending specialists to work',
    agent_handoff: 'handing work to a specialist',
    delegation: 'starting the specialist',
    agent_handback: 'reading a specialist\'s report',
  }[last?.type] || 'reading your message'
  return (
    <div className="working" data-testid="working">
      <span className="spinner" /> agent is {label}… <span className="mono">{Math.floor((now - start) / 1000)}s</span>
    </div>
  )
}

const HANDBACK_LABEL = {
  done: 'done', partial: 'partly refused', blocked: 'refused', refused: 'handoff refused', failed: 'failed',
}

export default function ChatPanel({ user, events, userMessages, objectives, agents = [], mode, busy, onSend }) {
  const team = mode === 'team'
  const names = Object.fromEntries(agents.map((a) => [a.id, a.name]))
  const lead = (id) => names[id] || (id === 'assistant-agent' ? 'Assistant' : id)
  const who = (id) => (id && names[id] ? <span className="who">{names[id]}</span> : null)
  const [text, setText] = useState('')
  const scroller = useRef(null)

  const decisionsById = useMemo(() => {
    const m = {}
    for (const e of events) if (e.type === 'decision' || e.type === 'delegation') m[e.payload.request_id] = e.payload
    return m
  }, [events])

  // Interleave the user's own messages with the agent events that followed them.
  const items = useMemo(() => {
    const out = []
    let u = 0
    for (const e of events) {
      while (u < userMessages.length && userMessages[u].afterSeq <= e.seq) out.push({ kind: 'user', ...userMessages[u++] })
      if (['agent_plan', 'agent_thinking', 'request_sent', 'agent_escalate', 'agent_dispatch', 'agent_handoff', 'agent_handback', 'agent_message'].includes(e.type)) out.push({ kind: e.type, event: e })
    }
    while (u < userMessages.length) out.push({ kind: 'user', ...userMessages[u++] })
    return out
  }, [events, userMessages])

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' })
  }, [items.length, Object.keys(decisionsById).length])

  const submit = (t) => {
    if (busy || !t.trim()) return
    onSend(t)
    setText('')
  }

  return (
    <section className="panel chat">
      <div className="panel-head">
        <h2>Chat</h2>
        {user && <span className="badge user-badge" data-testid="chat-user">acting for <b>{user.name}</b> · {user.role}</span>}
      </div>

      <div className="presets">
        {objectives.map((o) => (
          <button key={o.id} data-objective={o.id} className="preset" disabled={busy} title={o.note} onClick={() => submit(o.text)}>
            {o.label}
          </button>
        ))}
      </div>

      <div className="transcript" ref={scroller} data-testid="transcript">
        {items.length === 0 && (
          <div className="empty">
            {mode === 'adaptive'
              ? 'Pick a task above or type one. The assistant handles simple tasks itself and adds specialists only when a task needs them — several can work in parallel. The broker decides every handoff and every data request; watch it on the right.'
              : team
              ? 'Pick a team task above or type one. The orchestrator plans and hands work to specialists; the broker decides every handoff and every data request — watch it on the right.'
              : 'Pick an objective above or type a task. The agent plans first; complex tasks are handed to the agent team. Every handoff and data request goes through the broker — watch it on the right.'}
          </div>
        )}
        {items.map((it, i) => {
          if (it.kind === 'user') return <div key={i} className="msg user-msg">{it.text}</div>
          const p = it.event.payload
          if (it.kind === 'agent_plan') {
            return (
              <div key={i} className="plan" data-testid="plan">
                <div className="plan-title">{p.agent_id === 'orchestrator' ? 'Orchestrator plan' : 'Plan'}</div>
                <ol>{p.steps.map((s, j) => <li key={j}>{s}</li>)}</ol>
              </div>
            )
          }
          if (it.kind === 'agent_dispatch') {
            return (
              <div key={i} className="dispatch" data-testid="dispatch" data-parallel={p.parallel}>
                ⇉ <b>Assistant</b> {p.parallel > 1 ? <>sent <b>{p.parallel} specialists</b> to work in parallel</> : <>handed one part to a specialist</>}
              </div>
            )
          }
          if (it.kind === 'agent_escalate') {
            return (
              <div key={i} className="escalate" data-testid="escalate">
                ⇪ <b>Assistant</b> → <b>Orchestrator</b> · {p.reason}
              </div>
            )
          }
          if (it.kind === 'agent_thinking') return <div key={i} className="thinking">{who(p.agent_id)}{p.text}</div>
          if (it.kind === 'agent_handoff') {
            const d = decisionsById[p.delegation_id]
            const state = !d ? 'pending' : d.result === 'allow' ? 'allow' : 'deny'
            return (
              <div key={i} className={`handoff ${state}`} data-testid="handoff" data-delegation-id={p.delegation_id} data-to={p.to_agent}>
                <div className="handoff-head">
                  <span>🤝 <b>{lead(p.from_agent)}</b> → <b>{names[p.to_agent] || p.to_agent}</b></span>
                  <span className="req-result">
                    {state === 'pending' && 'broker deciding…'}
                    {state === 'allow' && 'handoff approved'}
                    {state === 'deny' && `handoff refused · ${d.rule_fired}`}
                  </span>
                </div>
                <div className="handoff-task">“{p.task}”</div>
                {state === 'deny' && <div className="req-reason">broker: {d.reason}</div>}
              </div>
            )
          }
          if (it.kind === 'agent_handback') {
            return (
              <details key={i} className={`handback status-${p.status}`} data-testid="handback" data-from={p.from_agent}>
                <summary>↩ <b>{names[p.from_agent] || p.from_agent}</b> reported back to the {lead(p.to_agent)} · {HANDBACK_LABEL[p.status] || p.status}</summary>
                <div className="handback-body" dangerouslySetInnerHTML={md(p.summary)} />
              </details>
            )
          }
          if (it.kind === 'request_sent') {
            const d = decisionsById[p.request_id]
            const state = !d ? 'pending' : d.result
            return (
              <div key={i} className={`req-line ${state}`} data-request-id={p.request_id}>
                <span className="req-icon">{state === 'allow' ? '✓' : state === 'pending' ? '…' : '✕'}</span>
                {who(p.agent_id)}
                <code>{describeRequest(p)}</code>
                <span className="req-result">
                  {state === 'allow' && `${d.row_count} rows`}
                  {state === 'deny' && `denied · ${d.rule_fired}`}
                  {state === 'error' && 'error'}
                </span>
                {d && d.result !== 'allow' && <div className="req-reason">broker: {d.reason}</div>}
              </div>
            )
          }
          if (it.kind === 'agent_message') {
            return <div key={i} className={`msg agent-msg ${p.error ? 'error' : ''}`} data-testid="agent-message" dangerouslySetInnerHTML={md(p.text)} />
          }
          return null
        })}
        {busy && <Working events={events} />}
      </div>

      <form className="composer" onSubmit={(e) => { e.preventDefault(); submit(text) }}>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={busy ? 'Agent is working…' : `Ask the agent (acting for ${user?.name ?? '…'})`}
          disabled={busy}
          data-testid="chat-input"
        />
        <button type="submit" disabled={busy || !text.trim()}>Send</button>
      </form>
    </section>
  )
}
