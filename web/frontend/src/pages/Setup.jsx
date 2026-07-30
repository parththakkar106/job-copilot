import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'

// First-run checklist. Every step here is something the console can already do on another
// tab, so this page is a router into existing UI rather than a second way of editing
// things — the setup pain was never a missing feature, it was that nothing told you which
// order to do them in or when you were finished.
//
// Steps flagged `manual` cannot be automated at all (Chrome requires a human to load an
// unpacked extension; BotFather is a conversation). They are shown rather than hidden, with
// a re-check button, because a checklist that quietly skips the hard parts is what made the
// README feel like a wall of text.

function Step({ step, index, onGo, onRecheck, busy }) {
  const { id, title, detail, done, manual, unfilled, link } = step
  return (
    <li className={`setup-step ${done ? 'is-done' : ''}`}>
      <div className="setup-mark" aria-hidden="true">{done ? '✓' : index + 1}</div>
      <div className="setup-body">
        <div className="setup-head">
          <h3>{title}</h3>
          {manual && <span className="setup-tag">needs you</span>}
          {step.blocking && !step.minimum_met && <span className="setup-tag req">required</span>}
        </div>
        <p className="setup-detail">{detail}</p>

        {/* The unfilled list is the single most useful thing on this page: it turns "fill in
            your profile" into a finite, countable list of what is actually left. */}
        {unfilled?.length > 0 && (
          <details className="setup-holes">
            <summary>{unfilled.length} field{unfilled.length === 1 ? '' : 's'} still unset</summary>
            <ul>{unfilled.map((f) => <li key={f}><code>{f}</code></li>)}</ul>
          </details>
        )}

        <div className="setup-actions">
          {link && !done && (
            <button className="btn btn-primary" onClick={() => onGo(link)}>
              {id === 'profile' ? 'Fill this in' : 'Open'}
            </button>
          )}
          {manual && (
            <button className="btn" disabled={busy} onClick={onRecheck}>
              {busy ? 'Checking…' : 'I did this, re-check'}
            </button>
          )}
        </div>
      </div>
    </li>
  )
}

export default function Setup({ onGo }) {
  const notify = useToast()
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async (announce) => {
    setBusy(true)
    try {
      const d = await api.setupStatus()
      setData(d)
      setErr('')
      if (announce) notify(d.ready ? 'Setup looks complete.' : 'Re-checked.', d.ready ? 'success' : 'info')
    } catch (e) {
      setErr(e.message || 'could not reach the backend')
    } finally {
      setBusy(false)
    }
  }, [notify])

  useEffect(() => { load(false) }, [load])

  if (err) return <div className="card"><p className="muted">Setup status unavailable: {err}</p></div>
  if (!data) return <div className="card"><p className="muted">Checking your setup…</p></div>

  const pct = Math.round((data.done_count / data.total) * 100)

  return (
    <div className="setup">
      <div className="setup-intro card">
        <h2>Let's get you set up</h2>
        <p className="muted">
          Five steps, and only the first is required. Everything here is editable later —
          nothing is locked in by finishing quickly.
        </p>
        <div className="setup-bar"><div className="setup-bar-fill" style={{ width: `${pct}%` }} /></div>
        <p className="setup-count">{data.done_count} of {data.total} done</p>
        {data.ready && (
          <p className="setup-ready">
            The essentials are in place — you can start using the console whenever you like.
          </p>
        )}
      </div>

      <ol className="setup-list">
        {data.steps.map((s, i) => (
          <Step key={s.id} step={s} index={i} onGo={onGo}
                onRecheck={() => load(true)} busy={busy} />
        ))}
      </ol>

      <div className="card setup-foot">
        <button className="btn" disabled={busy} onClick={() => load(true)}>
          {busy ? 'Checking…' : 'Re-check everything'}
        </button>
        <span className="muted">
          Your details are saved to <code>data/profile.json</code>, which is gitignored — filling
          this in cannot commit anything personal.
        </span>
      </div>
    </div>
  )
}
