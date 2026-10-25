import React, { useEffect, useMemo, useState } from 'react'
import { describeRequest } from './ChatPanel.jsx'

// Adaptive mode: the assistant does simple work itself and adds specialists
// only when a task needs them — several can work at once. A specialist row
// appears only when the broker APPROVES its handoff (a real delegation event);
// a refused handoff never starts a specialist, so it is listed as "not started". Events are
// replayed strictly in arrival order, so interleaved events from parallel
// specialists show up as several lanes busy at the same time.
const VISUAL = ['agent_dispatch', 'agent_handoff', 'broker_check', 'delegation', 'request_sent', 'decision',
  'result_returned', 'agent_handback', 'agent_message']
const STAGES = [
  ['token', 'Token'],
  ['delegation', 'Delegation'],
  ['agent_scope', 'Agent scope'],
  ['user_rights', 'User rights'],
]
const STATUS_LABEL = {
  handoff: 'receiving task…', working: 'working', done: 'reported back',
  partial: 'reported back · partly refused', blocked: 'reported back · refused', refused: 'handoff refused', failed: 'failed',
}
const BUSY = ['handoff', 'working']

function holdAfter(e) {
  if (!e) return 0
  const deny = e.payload?.result && e.payload.result !== 'allow'
  return {
    agent_dispatch: 1000, agent_handoff: 650, broker_check: 250, request_sent: 400, result_returned: 450,
    agent_handback: 800, delegation: deny ? 1400 : 500, decision: deny ? 1400 : 500,
  }[e.type] ?? 0
}

const EMPTY = { rows: [], pending: {}, refused: [], self: null, reqRow: {}, broker: null, db: '', dbKey: 0, main: 'idle', parallel: 0, feed: [], history: [] }

function apply(s, e) {
  const p = e.payload
  const rows = s.rows.map((r) => ({ ...r }))
  const row = (id) => rows.find((r) => r.id === id)
  const feed = (item) => [{ ...item, key: `${e.seq}` }, ...s.feed].slice(0, 6)
  const setReq = (rowId, fn) => {
    if (rowId === 'self') return { self: s.self && fn(s.self) }
    const r = row(rowId)
    if (r?.req) r.req = fn(r.req)
    return { rows }
  }
  switch (e.type) {
    case 'agent_dispatch':
      return { ...s, main: 'dispatch', parallel: p.parallel,
        feed: feed({ kind: 'info', text: p.parallel > 1 ? `Assistant sent ${p.parallel} specialists to work in parallel` : 'Assistant handed one part to a specialist' }) }
    case 'agent_handoff': {
      // Asked for, not yet started: the broker decides first.
      const pending = { ...s.pending, [p.delegation_id]: { id: p.delegation_id, agent: p.to_agent, task: p.task } }
      return { ...s, pending, broker: { id: p.delegation_id, rowId: p.delegation_id, kind: 'delegation', checks: {} } }
    }
    case 'broker_check': {
      const rowId = s.reqRow[p.request_id] || (s.pending[p.request_id] || row(p.request_id) ? p.request_id : null)
      if (!rowId) return s
      const b = s.broker?.id === p.request_id ? s.broker : { id: p.request_id, rowId, kind: p.kind || 'request', checks: {} }
      return { ...s, broker: { ...b, checking: p.stage, checks: { ...b.checks, [p.stage]: p.passed }, decision: null } }
    }
    case 'delegation': {
      const ask = s.pending[p.request_id]
      if (!ask) return s
      const { [p.request_id]: _, ...pending } = s.pending
      const ok = p.result === 'allow'
      const broker = { ...(s.broker || {}), id: p.request_id, rowId: p.request_id, kind: 'delegation', checking: null, decision: p }
      if (!ok) {
        const who = { ...ask, n: 1 }
        return { ...s, pending, broker, refused: [...s.refused, { ...ask, reason: p.reason }],
          feed: feed({ kind: 'deny', who, text: 'handoff refused — never started', rule: p.rule_fired, reason: p.reason }),
          history: [...s.history, { id: p.request_id, kind: 'delegation', who, ok }] }
      }
      // Approved: only now does the specialist exist and start working.
      const r = { ...ask, n: s.rows.filter((x) => x.agent === ask.agent).length + 1, status: 'working', lane: 'go', laneKey: 1, req: null }
      return { ...s, pending, broker, rows: [...rows, r],
        feed: feed({ kind: 'allow', who: r, text: `added · handoff approved, bound to ${p.on_behalf_of}` }),
        history: [...s.history, { id: p.request_id, kind: 'delegation', who: r, ok }] }
    }
    case 'request_sent': {
      const rowId = p.delegation_id || 'self'
      const req = { id: p.request_id, req: p, phase: 'travel', decision: null, rows: null }
      const reqRow = { ...s.reqRow, [p.request_id]: rowId }
      if (rowId === 'self') return { ...s, reqRow, self: req, main: 'own' }
      const r = row(rowId)
      if (!r) return s
      r.req = req
      return { ...s, rows, reqRow }
    }
    case 'decision': {
      const rowId = s.reqRow[p.request_id]
      if (!rowId) return s
      const ok = p.result === 'allow'
      return { ...s, ...setReq(rowId, (q) => ({ ...q, phase: ok ? 'to-db' : 'denied', decision: p })),
        broker: { ...(s.broker || {}), id: p.request_id, rowId, kind: 'request', checking: null, decision: p },
        db: ok ? 'go' : '', dbKey: s.dbKey + 1 }
    }
    case 'result_returned': {
      const rowId = s.reqRow[p.request_id]
      if (!rowId) return s
      const target = rowId === 'self' ? s.self : row(rowId)?.req
      if (!target) return s
      const ok = target.decision?.result === 'allow'
      const who = rowId === 'self' ? null : row(rowId)
      return { ...s, ...setReq(rowId, (q) => ({ ...q, phase: ok ? 'returning' : 'denied', rows: p.row_count ?? null })),
        db: ok ? 'back' : '', dbKey: s.dbKey + 1,
        feed: feed({ kind: ok ? 'allow' : 'deny', who, text: `${describeRequest(target.req)} · ${ok ? `${p.row_count ?? 0} rows` : 'denied'}`,
          rule: ok ? null : target.decision?.rule_fired, reason: ok ? null : target.decision?.reason }),
        history: [...s.history, { id: p.request_id, kind: 'request', who, ok, table: target.req.table, rows: p.row_count }] }
    }
    case 'agent_handback': {
      const r = row(p.delegation_id)
      if (!r) return s
      r.status = p.status
      r.lane = 'back'
      r.laneKey += 1
      return { ...s, rows, feed: feed({ kind: p.status === 'done' ? 'allow' : 'info', who: r, text: `reported back · ${STATUS_LABEL[p.status] || p.status}` }) }
    }
    case 'agent_message':
      return { ...s, main: 'answer', feed: feed({ kind: 'info', text: 'Assistant wrote the answer — see the chat' }) }
    default:
      return s
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

function track(req) {
  if (!req) return ''
  return req.phase === 'travel' ? 'go' : req.phase === 'returning' ? 'back' : ''
}

export default function AdaptiveSystemView({ user, events, agents }) {
  const names = Object.fromEntries(agents.map((a) => [a.id, a.name]))
  const visual = useMemo(() => events.filter((e) => VISUAL.includes(e.type)), [events])
  const [cursor, setCursor] = useState(0)
  const [state, setState] = useState(EMPTY)
  const countdown = useTokenCountdown()

  useEffect(() => {
    if (cursor >= visual.length) return
    const t = setTimeout(() => {
      setState((s) => apply(s, visual[cursor]))
      setCursor((c) => c + 1)
    }, cursor === 0 ? 0 : holdAfter(visual[cursor - 1]))
    return () => clearTimeout(t)
  }, [cursor, visual])

  const s = state
  const label = (r) => `${names[r.agent] || r.agent}${r.n && s.rows.filter((x) => x.agent === r.agent).length > 1 ? ` #${r.n}` : ''}`
  const busy = s.rows.filter((r) => BUSY.includes(r.status)).length
  const queued = visual.length - cursor
  const b = s.broker
  const bDenied = b?.decision && b.decision.result !== 'allow'
  const brokerPhase = !b ? '' : bDenied ? 'denied' : b.decision ? 'to-db' : 'checking'
  const brokerFor = !b ? null : b.rowId === 'self' ? 'the assistant'
    : (() => { const r = s.rows.find((x) => x.id === b.rowId) || s.pending[b.rowId] || s.refused.find((x) => x.id === b.rowId); return r ? (names[r.agent] || r.agent) : null })()
  const mainSub = busy > 1 ? `${busy} specialists working in parallel`
    : busy === 1 ? 'waiting for a specialist'
      : { dispatch: 'handing out work…', own: 'fetching data itself', answer: 'wrote the answer' }[s.main] || 'decides who does what'
  const nRows = 1 + s.rows.length

  return (
    <section className="panel system" data-testid="system-view">
      <div className="panel-head">
        <h2>System view · adaptive team</h2>
        <div className="sys-meta">
          {user && <span className="badge user-badge">on behalf of <b>{user.name}</b></span>}
          <span className="badge" data-testid="specialist-count" title="Specialists added for this conversation">
            {s.rows.length === 0 ? 'no specialists yet' : `${s.rows.length} specialist${s.rows.length === 1 ? '' : 's'}`}
            {busy > 1 && <> · <b>{busy} in parallel</b></>}
          </span>
          {countdown && <span className="badge token-badge" title="Assistant token lifetime (signed by the broker)">token ⏱ {countdown}</span>}
          {queued > 0 && <span className="badge queued" title="Real events received but not yet animated">{queued} queued</span>}
        </div>
      </div>

      <div className={`team-pipeline adaptive ${bDenied ? 'is-denied' : ''}`} style={{ gridTemplateRows: `repeat(${nRows}, auto)` }}>
        <div className={`node orch ${s.main === 'dispatch' ? 'sending' : ''} ${busy > 0 ? 'waiting' : ''} ${s.main === 'answer' ? 'receiving' : ''}`}
             style={{ gridRow: `1 / span ${nRows}`, gridColumn: 1 }} data-testid="main-agent-node">
          <div className="node-title">Assistant</div>
          <div className="node-sub">{mainSub}</div>
          <div className="node-foot">does simple work itself; adds specialists only when needed</div>
        </div>

        {/* The assistant's own, direct path to the broker. */}
        <div className="track self-lane" style={{ gridRow: 1, gridColumn: '2 / span 3' }} data-testid="self-lane">
          <span className="lane-label">its own requests</span>
          <span key={`${s.self?.id}-${track(s.self)}`} className={`packet ${track(s.self)}`} />
          {s.self?.phase === 'denied' && <span className="blocked">✕</span>}
        </div>

        {s.rows.map((r, i) => (
          <React.Fragment key={r.id}>
            <div className="track lane" style={{ gridRow: i + 2, gridColumn: 2 }}>
              <span key={`${r.id}-lane-${r.lane}-${r.laneKey}`} className={`card-packet ${r.lane} ${r.lane === 'back' ? r.status : ''}`} />
              {r.status === 'refused' && <span className="blocked">✕</span>}
            </div>
            <div className={`node sub spawned status-${r.status} ${BUSY.includes(r.status) ? 'active' : ''}`} style={{ gridRow: i + 2, gridColumn: 3 }}
                 data-agent={r.agent} data-status={r.status} data-delegation-id={r.id}>
              <div className="node-title">{label(r)}</div>
              <div className="sub-status">{STATUS_LABEL[r.status] || r.status}</div>
              <div className="sub-task" title={r.task}>{r.task}</div>
              {r.req && BUSY.includes(r.status) && <div className="sub-req mono">{describeRequest(r.req.req)}</div>}
            </div>
            <div className="track" style={{ gridRow: i + 2, gridColumn: 4 }}>
              <span key={`${r.req?.id}-${track(r.req)}`} className={`packet ${track(r.req)}`} />
              {r.req?.phase === 'denied' && r.status !== 'refused' && <span className="blocked">✕</span>}
            </div>
          </React.Fragment>
        ))}

        <div className={`node broker ${brokerPhase}`} style={{ gridRow: `1 / span ${nRows}`, gridColumn: 5 }} data-testid="broker-node">
          <div className="node-title">Broker</div>
          <div className="node-sub">{brokerFor ? <>{b.kind === 'delegation' ? 'deciding a handoff to' : 'checking a request from'} <b>{brokerFor}</b></> : 'sole enforcement point'}</div>
          <ul className="checks">
            {STAGES.map(([key, lbl]) => {
              const v = b?.checks?.[key]
              const cls = v === true ? 'pass' : v === false ? 'fail' : b?.checking === key ? 'running' : 'idle'
              return <li key={key} className={`check ${cls}`} data-stage={key}><span className="light" /> {lbl}</li>
            })}
          </ul>
        </div>
        <div className="track" style={{ gridRow: `1 / span ${nRows}`, gridColumn: 6 }}>
          <span key={`db-${s.dbKey}`} className={`packet ${s.db}`} />
        </div>
        <div className={`node db ${s.db ? 'hit' : ''}`} style={{ gridRow: `1 / span ${nRows}`, gridColumn: 7 }}>
          <div className="node-title">Database</div>
          <div className="node-sub">backend network</div>
          <div className="node-foot">only the broker connects</div>
        </div>
      </div>

      {s.rows.length === 0 && (
        <div className="idle-note adaptive-note">No specialists yet — they are added only when a task needs them.</div>
      )}
      {s.refused.length > 0 && (
        <div className="not-started" data-testid="not-started">
          <span className="muted">Not started (handoff refused by the broker):</span>
          {s.refused.map((r) => <span key={r.id} className="chip deny" title={r.reason}>✕ {names[r.agent] || r.agent}</span>)}
        </div>
      )}

      {s.feed.length > 0 && (
        <ul className="activity" data-testid="activity">
          {s.feed.map((f) => (
            <li key={f.key} className={`act ${f.kind}`}>
              {f.who && <b>{label(f.who)} · </b>}{f.text}
              {f.rule && <div className="verdict-reason"><span className="rule">{f.rule}</span> {f.reason}</div>}
            </li>
          ))}
        </ul>
      )}

      {s.history.length > 0 && (
        <div className="history" data-testid="history">
          {s.history.map((h) => (
            <span key={h.id} className={`chip ${h.ok ? 'allow' : 'deny'}`}>
              {h.kind === 'delegation'
                ? `🤝 ${label(h.who)} ${h.ok ? '✓' : '✕'}`
                : `${h.ok ? '✓' : '✕'} ${h.who ? label(h.who) : 'Assistant'} · ${h.table}${h.rows != null ? ` · ${h.rows}` : ''}`}
            </span>
          ))}
        </div>
      )}
    </section>
  )
}
