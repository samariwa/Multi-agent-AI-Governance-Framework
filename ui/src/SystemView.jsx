import React, { useEffect, useMemo, useState } from 'react'
import { describeRequest } from './ChatPanel.jsx'

// Animates REAL events only. Events are applied strictly in arrival order; the
// only liberty taken is pacing — each step is held on screen briefly so a human
// can follow it. A "queued" counter shows when playback is behind live.
const VISUAL = ['request_sent', 'broker_check', 'decision', 'result_returned']
const STAGES = [
  ['token', 'Token', 'signature + expiry'],
  ['agent_scope', 'Agent scope', 'what this agent may do'],
  ['user_rights', 'User rights', 'what this user may do'],
]
const HOLD = { request_sent: 750, broker_check: 450, allow: 850, deny: 2400, error: 1200, result_returned: 800 }

function holdAfter(event) {
  if (!event) return 0
  if (event.type === 'decision') return HOLD[event.payload.result] ?? 800
  return HOLD[event.type] ?? 0
}

function apply(state, event) {
  const p = event.payload
  const a = state.active
  switch (event.type) {
    case 'request_sent':
      return { ...state, active: { id: p.request_id, req: p, phase: 'travel', checks: {}, decision: null, rows: null } }
    case 'broker_check':
      if (!a || a.id !== p.request_id) return state
      return { ...state, active: { ...a, phase: 'checking', checking: p.stage, checks: { ...a.checks, [p.stage]: p.passed } } }
    case 'decision':
      if (!a || a.id !== p.request_id) return state
      return { ...state, active: { ...a, phase: p.result === 'allow' ? 'to-db' : 'denied', checking: null, decision: p } }
    case 'result_returned': {
      if (!a || a.id !== p.request_id) return state
      const done = { ...a, phase: a.decision?.result === 'allow' ? 'returning' : 'denied', rows: p.row_count ?? null }
      return { active: done, history: [...state.history, done] }
    }
    default:
      return state
  }
}

function useTokenCountdown() {
  const [exp, setExp] = useState(null)
  const [now, setNow] = useState(Date.now() / 1000)
  useEffect(() => {
    const load = () => fetch('/api/token').then((r) => r.json()).then((t) => setExp(t.exp)).catch(() => {})
    load()
    const poll = setInterval(load, 30000)
    const tick = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => { clearInterval(poll); clearInterval(tick) }
  }, [])
  if (!exp) return null
  const left = Math.max(0, Math.floor(exp - now))
  return `${String(Math.floor(left / 60)).padStart(2, '0')}:${String(left % 60).padStart(2, '0')}`
}

export default function SystemView({ user, events }) {
  const visual = useMemo(() => events.filter((e) => VISUAL.includes(e.type)), [events])
  const [cursor, setCursor] = useState(0)
  const [state, setState] = useState({ active: null, history: [] })
  const countdown = useTokenCountdown()

  useEffect(() => {
    if (cursor >= visual.length) return
    const t = setTimeout(() => {
      setState((s) => apply(s, visual[cursor]))
      setCursor((c) => c + 1)
    }, cursor === 0 ? 0 : holdAfter(visual[cursor - 1]))
    return () => clearTimeout(t)
  }, [cursor, visual])

  const a = state.active
  const queued = visual.length - cursor
  const denied = a?.phase === 'denied'
  const track1 = !a ? '' : a.phase === 'travel' ? 'go' : a.phase === 'returning' ? 'back' : ''
  const track2 = !a ? '' : a.phase === 'to-db' ? 'go' : a.phase === 'returning' ? 'back' : ''

  return (
    <section className="panel system" data-testid="system-view">
      <div className="panel-head">
        <h2>System view</h2>
        <div className="sys-meta">
          {user && <span className="badge user-badge">on behalf of <b>{user.name}</b></span>}
          {countdown && <span className="badge token-badge" title="Agent token lifetime (signed by the broker)">token ⏱ {countdown}</span>}
          {queued > 0 && <span className="badge queued" title="Real events received but not yet animated">{queued} queued</span>}
        </div>
      </div>

      <div className={`pipeline ${denied ? 'is-denied' : ''}`}>
        <div className={`node agent ${a && ['travel'].includes(a.phase) ? 'sending' : ''}`}>
          <div className="node-title">Agent</div>
          <div className="node-sub">LangGraph · no DB access</div>
          <div className="node-foot">sends structured requests</div>
        </div>

        <div className="track">
          <span key={`${a?.id}-1-${track1}`} className={`packet ${track1}`} />
          {denied && <span className="blocked">✕</span>}
        </div>

        <div className={`node broker ${a ? a.phase : ''}`} data-testid="broker-node">
          <div className="node-title">Broker</div>
          <div className="node-sub">sole enforcement point</div>
          <ul className="checks">
            {STAGES.map(([key, label, hint]) => {
              const v = a?.checks[key]
              const cls = v === true ? 'pass' : v === false ? 'fail' : a?.checking === key ? 'running' : 'idle'
              return (
                <li key={key} className={`check ${cls}`} data-stage={key} title={hint}>
                  <span className="light" /> {label}
                </li>
              )
            })}
          </ul>
        </div>

        <div className="track">
          <span key={`${a?.id}-2-${track2}`} className={`packet ${track2}`} />
        </div>

        <div className={`node db ${a?.phase === 'to-db' || a?.phase === 'returning' ? 'hit' : ''} ${denied ? 'untouched' : ''}`}>
          <div className="node-title">Database</div>
          <div className="node-sub">PostgreSQL · backend network</div>
          <div className="node-foot">{denied ? 'never reached' : 'only the broker connects'}</div>
        </div>
      </div>

      <div className="current">
        {!a && <div className="idle-note">Waiting for the agent's first request…</div>}
        {a && (
          <div className={`verdict ${a.decision ? a.decision.result : 'pending'}`} data-testid="verdict" data-request-id={a.id} data-result={a.decision?.result ?? 'pending'}>
            <div className="verdict-req"><code>{describeRequest(a.req)}</code> <span className="obo">for {a.req.on_behalf_of}</span></div>
            {!a.decision && <div className="verdict-status">checking…</div>}
            {a.decision?.result === 'allow' && (
              <div className="verdict-status">ALLOWED{a.rows != null && <> · {a.rows} rows returned</>}</div>
            )}
            {a.decision && a.decision.result !== 'allow' && (
              <>
                <div className="verdict-status deny-stamp">DENIED</div>
                <div className="verdict-reason">
                  <span className="rule" data-testid="rule-fired">{a.decision.rule_fired}</span> {a.decision.reason}
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {state.history.length > 0 && (
        <div className="history" data-testid="history">
          {state.history.map((h) => (
            <span key={h.id} className={`chip ${h.decision?.result}`} title={h.decision?.reason || ''}>
              {h.decision?.result === 'allow' ? '✓' : '✕'} {h.req.table}
              {h.rows != null ? ` · ${h.rows}` : ''}
            </span>
          ))}
        </div>
      )}
    </section>
  )
}
