import React, { useEffect, useMemo, useState } from 'react'
import { describeRequest } from './ChatPanel.jsx'

// Agent-team view: Orchestrator ─handoff─▶ specialists ─requests─▶ Broker ─▶ Database.
// Like the single-agent view it animates REAL events only, in arrival order,
// with pacing so a human can follow each handover.
const VISUAL = ['agent_escalate', 'agent_plan', 'agent_handoff', 'broker_check', 'delegation', 'request_sent', 'decision', 'result_returned', 'agent_handback', 'agent_message']
const STAGES = [
  ['token', 'Token'],
  ['delegation', 'Delegation'],
  ['agent_scope', 'Agent scope'],
  ['user_rights', 'User rights'],
]
const STATUS_LABEL = {
  idle: 'idle', handoff: 'receiving task…', working: 'working', done: 'reported back',
  partial: 'reported back · partly refused', blocked: 'reported back · refused', refused: 'handoff refused', failed: 'failed',
}

function holdAfter(e) {
  if (!e) return 0
  const deny = e.payload?.result && e.payload.result !== 'allow'
  return {
    agent_escalate: 1600, agent_plan: 1400, agent_handoff: 1100, broker_check: 450, request_sent: 750, result_returned: 800, agent_handback: 1300,
    delegation: deny ? 2400 : 900, decision: deny ? 2400 : 850,
  }[e.type] ?? 0
}

function apply(s, e) {
  const p = e.payload
  const a = s.active
  const subs = { ...s.subs }
  const lanes = { ...s.lanes }
  switch (e.type) {
    case 'agent_escalate':
      return { ...s, active: { kind: 'escalate', id: `esc-${e.ts}`, reason: p.reason } }
    case 'agent_plan':
      if (p.agent_id !== 'orchestrator') return s
      return { ...s, plan: p.assignments || [], active: { kind: 'plan', id: `plan-${e.ts}`, assignments: p.assignments || [] } }
    case 'agent_message':
      if (p.agent_id !== 'orchestrator') return s
      return { ...s, active: { kind: 'synth', id: `synth-${e.ts}` } }
    case 'agent_handoff':
      subs[p.to_agent] = { status: 'handoff', task: p.task }
      lanes[p.to_agent] = 'go'
      return { ...s, subs, lanes, active: { kind: 'delegation', id: p.delegation_id, agent: p.to_agent, task: p.task, checks: {}, decision: null } }
    case 'broker_check':
      if (!a || a.id !== p.request_id) return s
      return { ...s, active: { ...a, checking: p.stage, checks: { ...a.checks, [p.stage]: p.passed } } }
    case 'delegation': {
      if (!a || a.id !== p.request_id) return s
      const ok = p.result === 'allow'
      subs[p.to_agent] = { ...subs[p.to_agent], status: ok ? 'working' : 'refused' }
      lanes[p.to_agent] = ''
      const done = { ...a, decision: p, checking: null }
      return { ...s, subs, lanes, active: done, history: [...s.history, done] }
    }
    case 'request_sent':
      lanes[p.agent_id] = ''
      return { ...s, lanes, active: { kind: 'request', id: p.request_id, agent: p.agent_id, req: p, phase: 'travel', checks: {}, decision: null, rows: null } }
    case 'decision':
      if (!a || a.id !== p.request_id) return s
      return { ...s, active: { ...a, phase: p.result === 'allow' ? 'to-db' : 'denied', checking: null, decision: p } }
    case 'result_returned': {
      if (!a || a.id !== p.request_id) return s
      const done = { ...a, phase: a.decision?.result === 'allow' ? 'returning' : 'denied', rows: p.row_count ?? null }
      return { ...s, active: done, history: [...s.history, done] }
    }
    case 'agent_handback':
      subs[p.from_agent] = { ...subs[p.from_agent], status: p.status }
      lanes[p.from_agent] = 'back'
      return { ...s, subs, lanes, active: { kind: 'handback', id: `${p.delegation_id}-back`, agent: p.from_agent, status: p.status, summary: p.summary } }
    default:
      return s
  }
}

function useTokenCountdown() {
  const [exp, setExp] = useState(null)
  const [now, setNow] = useState(Date.now() / 1000)
  useEffect(() => {
    const load = () => fetch('/api/token?agent=orchestrator').then((r) => r.json()).then((t) => setExp(t.exp)).catch(() => {})
    load()
    const poll = setInterval(load, 30000)
    const tick = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => { clearInterval(poll); clearInterval(tick) }
  }, [])
  if (!exp) return null
  const left = Math.max(0, Math.floor(exp - now))
  return `${String(Math.floor(left / 60)).padStart(2, '0')}:${String(left % 60).padStart(2, '0')}`
}

export default function TeamSystemView({ user, events, agents }) {
  const specialists = agents.filter((a) => a.id !== 'orchestrator')
  const names = Object.fromEntries(agents.map((a) => [a.id, a.name]))
  const visual = useMemo(() => events.filter((e) => VISUAL.includes(e.type)), [events])
  const [cursor, setCursor] = useState(0)
  const [state, setState] = useState({ subs: {}, lanes: {}, active: null, history: [], plan: [] })
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
  const isReq = a?.kind === 'request'
  const isDel = a?.kind === 'delegation'
  const denied = (isReq && a.phase === 'denied') || (isDel && a.decision && a.decision.result !== 'allow')
  const brokerPhase = !a ? '' : denied ? 'denied' : isReq ? (a.phase === 'travel' ? '' : a.phase === 'to-db' || a.phase === 'returning' ? 'to-db' : 'checking') : isDel ? (a.decision ? 'to-db' : 'checking') : ''
  const dbTrack = isReq ? (a.phase === 'to-db' ? 'go' : a.phase === 'returning' ? 'back' : '') : ''

  return (
    <section className="panel system" data-testid="system-view">
      <div className="panel-head">
        <h2>System view · agent team</h2>
        <div className="sys-meta">
          {user && <span className="badge user-badge">on behalf of <b>{user.name}</b></span>}
          {countdown && <span className="badge token-badge" title="Orchestrator token lifetime (signed by the broker)">orchestrator token ⏱ {countdown}</span>}
          {queued > 0 && <span className="badge queued" title="Real events received but not yet animated">{queued} queued</span>}
        </div>
      </div>

      <div className={`team-pipeline ${denied ? 'is-denied' : ''}`} style={{ gridTemplateRows: `repeat(${specialists.length}, auto)` }}>
        <div className={`node orch ${a?.kind === 'delegation' && !a.decision ? 'sending' : ''} ${['handback', 'escalate', 'plan', 'synth'].includes(a?.kind) ? 'receiving' : ''}`}
             style={{ gridRow: `1 / span ${specialists.length}` }} data-testid="orchestrator-node">
          <div className="node-title">Orchestrator</div>
          <div className="node-sub">{{ escalate: 'receiving the task…', plan: 'planning handoffs…', synth: 'writing the answer…' }[a?.kind] || 'plans & hands off'}</div>
          <div className="node-foot">no data access of its own</div>
        </div>

        {specialists.map((sp, i) => {
          const sub = state.subs[sp.id] || { status: 'idle' }
          const lane = state.lanes[sp.id] || ''
          const activeHere = a?.agent === sp.id
          const reqHere = isReq && activeHere
          const track = reqHere ? (a.phase === 'travel' ? 'go' : a.phase === 'returning' ? 'back' : '') : ''
          return (
            <React.Fragment key={sp.id}>
              <div className="track lane" style={{ gridRow: i + 1, gridColumn: 2 }}>
                <span key={`${sp.id}-lane-${lane}-${state.history.length}`} className={`card-packet ${lane} ${lane === 'back' ? sub.status : ''}`} />
                {sub.status === 'refused' && <span className="blocked">✕</span>}
              </div>
              <div className={`node sub status-${sub.status} ${activeHere ? 'active' : ''}`} style={{ gridRow: i + 1, gridColumn: 3 }}
                   data-agent={sp.id} data-status={sub.status}>
                <div className="node-title">{sp.name}</div>
                <div className="sub-status">{STATUS_LABEL[sub.status] || sub.status}</div>
                {sub.task && <div className="sub-task" title={sub.task}>{sub.task}</div>}
              </div>
              <div className="track" style={{ gridRow: i + 1, gridColumn: 4 }}>
                <span key={`${a?.id}-${sp.id}-${track}`} className={`packet ${track}`} />
                {reqHere && a.phase === 'denied' && <span className="blocked">✕</span>}
              </div>
            </React.Fragment>
          )
        })}

        <div className={`node broker ${brokerPhase}`} style={{ gridRow: `1 / span ${specialists.length}`, gridColumn: 5 }} data-testid="broker-node">
          <div className="node-title">Broker</div>
          <div className="node-sub">{isDel ? 'deciding a handoff' : 'sole enforcement point'}</div>
          <ul className="checks">
            {STAGES.map(([key, label]) => {
              const v = a?.checks?.[key]
              const cls = v === true ? 'pass' : v === false ? 'fail' : a?.checking === key ? 'running' : 'idle'
              return <li key={key} className={`check ${cls}`} data-stage={key}><span className="light" /> {label}</li>
            })}
          </ul>
        </div>
        <div className="track" style={{ gridRow: `1 / span ${specialists.length}`, gridColumn: 6 }}>
          <span key={`${a?.id}-db-${dbTrack}`} className={`packet ${dbTrack}`} />
        </div>
        <div className={`node db ${dbTrack ? 'hit' : ''} ${denied ? 'untouched' : ''}`} style={{ gridRow: `1 / span ${specialists.length}`, gridColumn: 7 }}>
          <div className="node-title">Database</div>
          <div className="node-sub">backend network</div>
          <div className="node-foot">only the broker connects</div>
        </div>
      </div>

      <div className="current">
        {!a && <div className="idle-note">Waiting for the orchestrator to hand out work…</div>}
        {a?.kind === 'escalate' && (
          <div className="verdict escalate-verdict" data-kind="escalate">
            <div className="verdict-req">⇪ <b>Assistant</b> → <b>Orchestrator</b></div>
            <div className="verdict-status">{a.reason}</div>
          </div>
        )}
        {a?.kind === 'plan' && (
          <div className="verdict escalate-verdict" data-kind="plan">
            <div className="verdict-req">🗂 <b>Orchestrator</b> planned {a.assignments.length} handoff{a.assignments.length === 1 ? '' : 's'}</div>
            <div className="verdict-status">{a.assignments.map((x) => names[x.agent] || x.agent).join(' → ')}</div>
          </div>
        )}
        {a?.kind === 'synth' && (
          <div className="verdict escalate-verdict" data-kind="synth">
            <div className="verdict-req">✍ <b>Orchestrator</b> combined the specialists' reports into one answer</div>
            <div className="verdict-status">REPLY SENT · see the chat</div>
          </div>
        )}
        {isDel && (
          <div className={`verdict ${a.decision ? (a.decision.result === 'allow' ? 'allow' : 'deny') : 'pending'}`} data-testid="verdict"
               data-kind="delegation" data-result={a.decision?.result ?? 'pending'}>
            <div className="verdict-req">🤝 <b>Handoff</b> Orchestrator → {names[a.agent]}: <span className="muted">“{a.task}”</span></div>
            {!a.decision && <div className="verdict-status">broker checking handoff…</div>}
            {a.decision?.result === 'allow' && <div className="verdict-status">HANDOFF APPROVED · narrower token issued, bound to {a.decision.on_behalf_of}</div>}
            {a.decision && a.decision.result !== 'allow' && (
              <>
                <div className="verdict-status deny-stamp">HANDOFF REFUSED</div>
                <div className="verdict-reason"><span className="rule" data-testid="rule-fired">{a.decision.rule_fired}</span> {a.decision.reason}</div>
              </>
            )}
          </div>
        )}
        {isReq && (
          <div className={`verdict ${a.decision ? a.decision.result : 'pending'}`} data-testid="verdict" data-kind="request" data-result={a.decision?.result ?? 'pending'}>
            <div className="verdict-req"><b>{names[a.agent]}</b> <code>{describeRequest(a.req)}</code> <span className="obo">for {a.req.on_behalf_of}</span></div>
            {!a.decision && <div className="verdict-status">checking…</div>}
            {a.decision?.result === 'allow' && <div className="verdict-status">ALLOWED{a.rows != null && <> · {a.rows} rows returned</>}</div>}
            {a.decision && a.decision.result !== 'allow' && (
              <>
                <div className="verdict-status deny-stamp">DENIED</div>
                <div className="verdict-reason"><span className="rule" data-testid="rule-fired">{a.decision.rule_fired}</span> {a.decision.reason}</div>
              </>
            )}
          </div>
        )}
        {a?.kind === 'handback' && (
          <div className={`verdict handback-verdict status-${a.status}`} data-kind="handback">
            <div className="verdict-req">↩ <b>{names[a.agent]}</b> handed its report back to the Orchestrator</div>
            <div className="verdict-status">{(STATUS_LABEL[a.status] || a.status).toUpperCase()}</div>
          </div>
        )}
      </div>

      {state.plan.length > 0 && (
        <ol className="team-plan" data-testid="team-plan">
          {state.plan.map((x, i) => {
            const st = state.subs[x.agent]?.status || 'idle'
            return (
              <li key={i} className={`status-${st}`}>
                <b>{names[x.agent] || x.agent}</b> <span className="muted">{x.task}</span>
                <span className="step-status">{st === 'idle' ? 'waiting' : STATUS_LABEL[st] || st}</span>
              </li>
            )
          })}
        </ol>
      )}

      {state.history.length > 0 && (
        <div className="history" data-testid="history">
          {state.history.map((h) => (
            <span key={h.id} className={`chip ${h.decision?.result === 'allow' ? 'allow' : 'deny'}`} title={h.decision?.reason || ''}>
              {h.kind === 'delegation'
                ? `🤝 ${names[h.agent]} ${h.decision?.result === 'allow' ? '✓' : '✕'}`
                : `${h.decision?.result === 'allow' ? '✓' : '✕'} ${names[h.agent]} · ${h.req.table}${h.rows != null ? ` · ${h.rows}` : ''}`}
            </span>
          ))}
        </div>
      )}
    </section>
  )
}
