import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'

const RESUME_TAGS = ['quant', 'ds', 'tech', 'ai']

// Greenhouse forms are often embedded on the company's own careers site (asana.com,
// careers.roblox.com, ...), where the form lives in a cross-origin iframe — the extension
// panel can't float and its dropdown clicks misfire. The legacy embed endpoint serves the
// SAME form standalone, where everything works. Route Greenhouse roles there; leave
// Lever/Ashby (already standalone) and anything without a job id on their stored URL.
const applyUrl = (rec) =>
  (rec.ats === 'greenhouse' && rec.ats_job_id)
    ? `https://boards.greenhouse.io/embed/job_app?token=${rec.ats_job_id}`
    : rec.url

export default function JobDrawer({ dedupKey, onClose, onChanged, bridgeAlive }) {
  const notify = useToast()
  const [rec, setRec] = useState(null)
  const [answers, setAnswers] = useState({})
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = async () => {
    try {
      const r = await api.job(dedupKey)
      setRec(r)
      const a = {}
      for (const q of r.questions || []) a[q.id] = q.answer ?? ''
      setAnswers(a)
      setDirty(false)
    } catch (e) { notify(e.message, 'error'); onClose() }
  }
  useEffect(() => { load() /* eslint-disable-next-line */ }, [dedupKey])

  if (!rec) {
    return (
      <div className="drawer-scrim" onClick={onClose}>
        <div className="drawer" onClick={(e) => e.stopPropagation()}><div className="empty">Loading…</div></div>
      </div>
    )
  }

  const setAns = (id, v) => { setAnswers((a) => ({ ...a, [id]: v })); setDirty(true) }

  const save = async () => {
    setSaving(true)
    try {
      // Empty string = deliberately blank (a resolved optional); the pipeline treats
      // "" as answered and only None as still-open, so this is intentional.
      const payload = (rec.questions || []).map((q) => ({ id: q.id, value: answers[q.id] ?? '' }))
      const r = await api.saveAnswers(dedupKey, payload)
      setRec(r); setDirty(false)
      notify(r._complete ? 'Saved — all questions answered' : 'Saved', 'success')
      onChanged?.()
    } catch (e) { notify(e.message, 'error', 6000) } finally { setSaving(false) }
  }

  /**
   * Run an action, then close. Pile moves pass no `ok` message: they're reversible and the
   * role visibly leaves the pile behind the drawer, so a toast per move was just noise.
   * Only real failures speak — and `confirm` is reserved for submitting, which isn't
   * reversible.
   */
  const act = async (fn, ok, confirmMsg) => {
    if (confirmMsg && !confirm(confirmMsg)) return
    try { const r = await fn(); if (ok) notify(ok(r), 'success'); onChanged?.(); onClose() }
    catch (e) { notify(e.message, 'error', 6000) }
  }

  const setResume = async (tag) => {
    try { await api.setResume(dedupKey, tag); notify(`Resume pinned: ${tag}`, 'success'); load() }
    catch (e) { notify(e.message, 'error') }
  }

  const complete = rec._complete
  const pinned = rec.resume_override

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <div className="drawer-title">{rec.company} · {rec.title}</div>
            <div className="drawer-meta">
              {rec.location || '—'} · <span className={`tag tag-${rec.status === 'approved' ? 'filled' : 'in_queue'}`}>{rec.status}</span>
              {rec.url && (applyUrl(rec) === rec.url
                ? <> · <a href={rec.url} target="_blank" rel="noreferrer">open ↗</a></>
                : <> · <a href={rec.url} target="_blank" rel="noreferrer">job description ↗</a>
                    {' · '}<a href={applyUrl(rec)} target="_blank" rel="noreferrer">fill form ↗</a></>)}
            </div>
          </div>
          <button className="btn btn-sm btn-ghost" onClick={onClose}>✕</button>
        </div>

        {rec.warnings?.length > 0 && (
          <div className="warn-box">
            <strong>Required fields the form flagged empty:</strong>
            <ul>{rec.warnings.map((w, i) => <li key={i}>{typeof w === 'string' ? w : (w.label || JSON.stringify(w))}</li>)}</ul>
          </div>
        )}

        <div className="drawer-section">
          <div className="section-label">Resume track</div>
          <div className="resume-picker">
            {RESUME_TAGS.map((t) => (
              <button key={t} className={`chip ${pinned === t ? 'chip-active' : ''}`} onClick={() => setResume(t)}>{t}</button>
            ))}
            <span className="hint">{pinned ? `pinned: ${pinned}` : `auto: ${rec.resume || 'best match'}`}</span>
          </div>
        </div>

        <div className="drawer-section">
          <div className="section-label">Questions ({(rec.questions || []).length})</div>
          {(rec.questions || []).length === 0 && <div className="hint">No open questions — deterministic fill covered everything.</div>}
          {(rec.questions || []).map((q) => (
            <div key={q.id} className="qa">
              <label className="qa-label">
                {q.label}
                {q.answer == null && <span className="qa-open">needs answer</span>}
                {/* The form already has a value here — Greenhouse's resume parser fills the
                    education block before the extension sees it, and those fields match no
                    taxonomy rule, so they were escalated as questions anyway. Saying so is
                    the difference between "type this out" and "confirm this". */}
                {q._on_form && <span className="qa-onform">already on the form</span>}
              </label>
              {q.draft && (answers[q.id] ?? '') === '' && (
                <button className="draft-chip" onClick={() => setAns(q.id, q.draft)}>
                  {q._on_form ? 'use what the form has ↴' : 'use draft ↴'}
                </button>
              )}
              {Array.isArray(q.options) && q.options.length > 0 ? (
                <select value={answers[q.id] ?? ''} onChange={(e) => setAns(q.id, e.target.value)}>
                  <option value="">— leave blank —</option>
                  {q.options.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              ) : (
                <textarea rows={q.kind === 'textarea' ? 4 : 2} value={answers[q.id] ?? ''}
                  onChange={(e) => setAns(q.id, e.target.value)}
                  placeholder="leave empty to answer as blank" />
              )}
            </div>
          ))}
        </div>

        {(rec.filled || []).length > 0 && (
          <div className="drawer-section">
            <div className="section-label">Auto-filled ({rec.filled.length})</div>
            <div className="hint" style={{ marginBottom: 8 }}>
              Deterministic answers the extension filled from your profile. Read-only here.
            </div>
            {rec.filled.map((f, i) => (
              <div key={i} className="qa qa-filled">
                <div className="qa-label">{f.label}</div>
                <div className="qa-value">{f.value === '' ? <span className="hint">— blank —</span> : String(f.value)}</div>
              </div>
            ))}
          </div>
        )}

        <div className="drawer-actions">
          <button className="btn btn-primary" disabled={!dirty || saving} onClick={save}>
            {saving ? 'Saving…' : 'Save answers'}
          </button>
          <button className="btn btn-warn" disabled={!complete || dirty || !bridgeAlive}
            title={!bridgeAlive ? 'bridge is down' : !complete ? 'answer everything first' : dirty ? 'save first' : ''}
            onClick={() => act(() => api.submitJob(dedupKey),
              (r) => `Submit requested (${r.approved_now} approved will be sent)`,
              'Submit this application for real? The extension will send the approved pile.')}>
            ⇪ Submit this one
          </button>
          {rec.status === 'approved' && (
            <button className="btn" onClick={() => act(() =>
              api.setState(dedupKey, 'held', 'set aside from web'))}>Hold</button>
          )}
          <button className="btn" onClick={() => act(() => api.setState(dedupKey, 'skipped'))}>Skip</button>
          <div className="spacer" />
          <button className="btn btn-ghost btn-danger"
            onClick={() => act(() => api.setState(dedupKey, 'blocked'))}>Not interested</button>
        </div>
      </div>
    </div>
  )
}
