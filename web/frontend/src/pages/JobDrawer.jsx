import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'

const RESUME_TAGS = ['quant', 'ds', 'tech', 'ai']

// The AI pass's actions (bridge/aipass.py decide()). Only fill and replace write to the form.
const AI_VERB = { fill: 'AI filled', replace: 'AI replaced', flag: 'flagged, not written', propose: 'suggested, not written' }
const WRITES = new Set(['fill', 'replace'])
// Tiers match aipass.py: below LOW_CONF (0.6) the bridge sets `low`. At REPLACE_MIN (0.8)
// the model may overwrite a value, so 0.8 and up counts as high.
const confTier = (d) => (d.low ? 'low' : (Number(d.confidence) >= 0.8 ? 'high' : 'mid'))
// A label reduced to what names the field, as pending.py's _warn_key does.
const labelKey = (s) => String(s || '').replace(/[✱*]/g, '').replace(/\s+/g, ' ').trim().toLowerCase()

// A filled value with an edit control. Phase 3 writes the saved value onto the form.
function FilledValue({ row, onSave }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const opts = row?.options || []
  if (!row) return null
  if (!editing) {
    return (
      <div className="filled-row">
        <div className="qa-value">{row.value === '' ? <span className="hint">— blank —</span> : String(row.value)}</div>
        {/* The resume is a file attached by track; change it with the picker above. */}
        {labelKey(row.label) !== 'resume' && (
          <button className="btn btn-sm btn-ghost" onClick={() => { setDraft(String(row.value ?? '')); setEditing(true) }}>Edit</button>
        )}
      </div>
    )
  }
  const save = async () => { if (await onSave(row.label, draft)) setEditing(false) }
  return (
    <div className="filled-row">
      {opts.length > 0 ? (
        <select value={draft} onChange={(e) => setDraft(e.target.value)} aria-label={`New value for ${row.label}`}>
          {!opts.includes(draft) && <option value={draft}>{draft || '— choose —'}</option>}
          {opts.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : (
        <textarea rows={2} value={draft} onChange={(e) => setDraft(e.target.value)} aria-label={`New value for ${row.label}`} />
      )}
      <button className="btn btn-sm btn-primary" onClick={save}>Save</button>
      <button className="btn btn-sm btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
    </div>
  )
}

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
  const aiDecisions = rec.ai_decisions || []
  // `filled` is phase 1's read-back of the whole form, so it also holds every value the AI
  // wrote. Those are listed under the AI pass with their confidence, so leave them out here.
  const aiWrote = new Set(aiDecisions
    .filter((d) => WRITES.has(d.action) && d.applied !== false).map((d) => labelKey(d.label)))
  const filledByKey = new Map((rec.filled || []).map((f) => [labelKey(f.label), f]))
  const filled = (rec.filled || []).filter((f) => !aiWrote.has(labelKey(f.label)))

  const editFilled = async (label, value) => {
    try {
      const r = await api.editFilled(dedupKey, label, value)
      setRec(r)
      notify(rec.status === 'approved' ? 'Saved. Approve the job again to send it.' : 'Saved', 'success')
      onChanged?.()
      return true
    } catch (e) { notify(e.message, 'error', 6000); return false }
  }

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

        {aiDecisions.length > 0 && (
          <div className="drawer-section">
            <div className="section-label">AI pass ({aiDecisions.length})</div>
            <div className="hint" style={{ marginBottom: 8 }}>
              What the AI fill + verify pass did on this form, least certain first.
            </div>
            {aiDecisions.map((d, i) => {
              const written = WRITES.has(d.action) && d.applied !== false
              return (
                <div key={d.id || i} className="qa qa-filled">
                  <div className="qa-label">
                    {d.label || d.id}
                    <span className={`ai-badge ai-${written ? 'wrote' : 'kept'}`}>
                      {written ? AI_VERB[d.action] : (WRITES.has(d.action) ? 'not written' : AI_VERB[d.action])}
                    </span>
                    <span className={`ai-conf ai-conf-${confTier(d)}`}
                      title="The model's own confidence in this value">
                      {Math.round((Number(d.confidence) || 0) * 100)}% confident
                    </span>
                    {d.hot && <span className="ai-badge ai-hot">check this</span>}
                  </div>
                  {written && filledByKey.has(labelKey(d.label))
                    ? <FilledValue row={filledByKey.get(labelKey(d.label))} onSave={editFilled} />
                    : <div className="qa-value">{d.value === '' ? <span className="hint">— blank —</span> : String(d.value)}</div>}
                  {d.was && (d.action === 'replace' || d.action === 'flag') && (
                    <div className="hint ai-note">
                      {d.action === 'replace' ? 'Was: ' : 'Form kept: '}{d.was}
                    </div>
                  )}
                  {d.why && <div className="hint ai-note">Reason: {d.why}</div>}
                  {WRITES.has(d.action) && d.applied === false && d.why_not && (
                    <div className="hint ai-note">Not written: {d.why_not}</div>
                  )}
                </div>
              )
            })}
          </div>
        )}

        {filled.length > 0 && (
          <div className="drawer-section">
            <div className="section-label">Auto-filled ({filled.length})</div>
            <div className="hint" style={{ marginBottom: 8 }}>
              Answers the extension filled from your profile. An edit is written onto the form when the job is sent.
            </div>
            {filled.map((f, i) => (
              <div key={i} className="qa qa-filled">
                <div className="qa-label">
                  {f.label}
                  {f.edited && <span className="ai-badge ai-wrote">your edit</span>}
                </div>
                <FilledValue row={f} onSave={editFilled} />
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
