import React, { useEffect, useRef, useState } from 'react'

// Rendered straight from the broker's append-only audit log (via the agent
// service's read-only proxy). It is re-read whenever a decision event arrives.
export default function AuditLog({ tick, sessionId }) {
  const [records, setRecords] = useState([])
  const [onlySession, setOnlySession] = useState(false)
  const seen = useRef(new Set())
  const [fresh, setFresh] = useState(new Set())

  useEffect(() => {
    const t = setTimeout(() => {
      fetch('/api/audit?n=300').then((r) => r.json()).then(({ records }) => {
        const newIds = new Set(records.filter((r) => !seen.current.has(r.request_id)).map((r) => r.request_id))
        if (seen.current.size > 0) setFresh(newIds)
        records.forEach((r) => seen.current.add(r.request_id))
        setRecords(records)
      }).catch(() => {})
    }, 150)
    return () => clearTimeout(t)
  }, [tick])

  const shown = (onlySession ? records.filter((r) => r.session_id === sessionId) : records).slice().reverse()
  const scope = onlySession ? records.filter((r) => r.session_id === sessionId) : records
  const allowed = scope.filter((r) => r.result === 'allow')
  const handoffs = scope.filter((r) => r.action === 'delegate').length
  const rowsRead = allowed.reduce((n, r) => n + (r.row_count || 0), 0)

  return (
    <section className="panel audit" data-testid="audit-log">
      <div className="panel-head">
        <h2>Audit log</h2>
        <div className="audit-stats">
          <span>{scope.length} requests</span>
          <span className="ok">{allowed.length} allowed</span>
          <span className="bad">{scope.length - allowed.length} denied</span>
          <span title="Total rows returned to agents">{rowsRead} rows read</span>
          {handoffs > 0 && <span title="Agent-to-agent handoffs decided by the broker">{handoffs} handoffs</span>}
          <label className="toggle">
            <input type="checkbox" checked={onlySession} onChange={(e) => setOnlySession(e.target.checked)} /> this session
          </label>
        </div>
      </div>
      <div className="audit-scroll">
        <table>
          <thead>
            <tr><th>time</th><th>agent</th><th>on behalf of</th><th>resource</th><th>action</th><th>verdict</th><th>rows</th><th>reason</th></tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.request_id} className={`${r.result} ${fresh.has(r.request_id) ? 'fresh' : ''}`} data-request-id={r.request_id}>
                <td className="mono">{new Date(r.ts).toLocaleTimeString()}</td>
                <td>{r.delegation_chain ? <span className="chain">{r.delegation_chain.join(' → ')}</span> : (r.agent_id ?? '—')}</td>
                <td><b>{r.on_behalf_of}</b></td>
                <td className="mono">
                  {r.action === 'delegate' ? <>handoff → {r.resource.delegate_to}</> : <>{r.resource.table}({r.resource.columns.join(', ')})</>}
                  {r.resource.filter_columns?.length > 0 && <span className="muted"> where {r.resource.filter_columns.join(', ')}</span>}
                </td>
                <td>{r.action}</td>
                <td><span className={`verdict-pill ${r.result}`}>{r.result}</span></td>
                <td className="mono">{r.row_count ?? '—'}</td>
                <td className="reason">{r.result === 'allow' ? '' : <><span className="rule">{r.rule_fired}</span> {r.reason}</>}</td>
              </tr>
            ))}
            {shown.length === 0 && <tr><td colSpan={8} className="muted">No requests yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  )
}
