import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'

const STATUSES = ['active', 'draft', 'stale']
const BLANK = { id: '', key: '', scope: 'common', company: '', answer: '', tags: '', status: 'active' }

function Editor({ row, onSave, onCancel }) {
  const [f, setF] = useState({
    ...row,
    tags: Array.isArray(row.tags) ? row.tags.join(', ') : (row.tags || ''),
    company: row.company || '',
  })
  const set = (k, v) => setF((x) => ({ ...x, [k]: v }))
  return (
    <div className="bank-editor">
      <div className="field-row">
        <label>Key<input value={f.key} onChange={(e) => set('key', e.target.value)} placeholder="why_company" /></label>
        <label>Scope
          <select value={f.scope} onChange={(e) => set('scope', e.target.value)}>
            <option value="common">common</option>
            <option value="company">company</option>
          </select>
        </label>
        {f.scope === 'company' && (
          <label>Company<input value={f.company} onChange={(e) => set('company', e.target.value)} placeholder="Ramp" /></label>
        )}
        <label>Status
          <select value={f.status} onChange={(e) => set('status', e.target.value)}>
            {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
      </div>
      <label className="full">Answer
        <textarea rows={5} value={f.answer} onChange={(e) => set('answer', e.target.value)} />
      </label>
      <label className="full">Tags (comma-separated)
        <input value={f.tags} onChange={(e) => set('tags', e.target.value)} placeholder="behavioral, motivation" />
      </label>
      <div className="editor-actions">
        <button className="btn btn-primary" onClick={() => onSave(f)}>Save</button>
        <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  )
}

export default function AnswerBank() {
  const notify = useToast()
  const [rows, setRows] = useState([])
  const [editing, setEditing] = useState(null) // id or '__new__'
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    try { setRows((await api.bank()).answers) }
    catch (e) { notify(e.message, 'error') } finally { setLoading(false) }
  }
  useEffect(() => { load() /* eslint-disable-next-line */ }, [])

  const save = async (f) => {
    try {
      await api.saveBank({
        id: f.id || undefined, key: f.key, scope: f.scope,
        company: f.scope === 'company' ? f.company : null,
        answer: f.answer, tags: f.tags, status: f.status,
      })
      notify('Saved', 'success'); setEditing(null); load()
    } catch (e) { notify(e.message, 'error', 6000) }
  }

  const remove = async (id) => {
    if (!confirm(`Delete answer "${id}"?`)) return
    try { await api.deleteBank(id); notify('Deleted', 'success'); load() }
    catch (e) { notify(e.message, 'error') }
  }

  return (
    <div className="bank">
      <div className="page-head">
        <h2>Answer bank</h2>
        <button className="btn btn-primary" onClick={() => setEditing('__new__')}>+ New answer</button>
      </div>
      <p className="page-hint">Reusable answers served at apply-time. A company-scoped active answer beats a common one; only <b>active</b> answers are served.</p>

      {editing === '__new__' && <div className="card"><Editor row={BLANK} onSave={save} onCancel={() => setEditing(null)} /></div>}

      {loading ? <div className="empty">Loading…</div> : rows.map((r) => (
        <div key={r.id} className="card bank-card">
          {editing === r.id ? (
            <Editor row={r} onSave={save} onCancel={() => setEditing(null)} />
          ) : (
            <>
              <div className="bank-card-head">
                <div>
                  <span className="mono">{r.key}</span>
                  <span className={`badge badge-${r.status}`}>{r.status}</span>
                  <span className="badge">{r.company || 'common'}</span>
                </div>
                <div className="bank-card-meta">
                  uses {r.uses ?? 0} · last {r.last_used || '—'}
                  <button className="btn btn-sm" onClick={() => setEditing(r.id)}>Edit</button>
                  <button className="btn btn-sm btn-ghost" onClick={() => remove(r.id)}>Delete</button>
                </div>
              </div>
              <div className="bank-answer">{r.answer?.trim()}</div>
              {r.tags?.length > 0 && <div className="bank-tags">{r.tags.map((t) => <span key={t} className="tag-mini">{t}</span>)}</div>}
            </>
          )}
        </div>
      ))}
    </div>
  )
}
