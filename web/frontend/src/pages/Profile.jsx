import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'

// Deep clone + set a value at a path like ['identity','email'].
function setAt(obj, path, value) {
  const next = structuredClone(obj)
  let cur = next
  for (let i = 0; i < path.length - 1; i++) cur = cur[path[i]]
  cur[path[path.length - 1]] = value
  return next
}

function titleize(k) {
  return k.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

// A blank item shaped like `sample`, for the Add button of a list. Notes keep their text,
// because they explain the fields, and everything else is emptied.
function blank(sample, key = '') {
  if (key.startsWith('_')) return sample
  if (Array.isArray(sample)) return []
  if (sample && typeof sample === 'object') {
    return Object.fromEntries(Object.entries(sample).map(([k, v]) => [k, blank(v, k)]))
  }
  if (typeof sample === 'boolean') return false
  if (typeof sample === 'number') return 0
  return ''
}

// A short name for one list item, from the first identifying field it has.
function itemName(item, i) {
  const name = ['employer', 'company', 'school', 'name', 'title', 'tag']
    .map((k) => item[k]).find((v) => typeof v === 'string' && v && v !== 'FILL_ME')
  return name || `Item ${i + 1}`
}

// A variable-length list. Records get a card each, and plain values get one input each.
// Every item can be removed, and records can be moved, because order is priority in some
// lists (resume_tracks picks the first track whose keywords match).
function ListField({ path, k, value, onChange }) {
  const label = titleize(k)
  const move = (i, d) => {
    const next = [...value]
    ;[next[i], next[i + d]] = [next[i + d], next[i]]
    onChange(path, next)
  }
  const remove = (i) => onChange(path, value.filter((_, j) => j !== i))
  const add = () => onChange(path, [...value, value.length ? blank(value[0]) : ''])
  const records = value.some((v) => v && typeof v === 'object')

  return (
    <div className="pf-subsection full">
      <div className="pf-subtitle">{label}</div>
      {records ? value.map((item, i) => (
        <div key={i} className="pf-item">
          <div className="pf-item-head">
            <b>{itemName(item, i)}</b>
            <span className="pf-item-actions">
              <button type="button" className="btn" disabled={i === 0} onClick={() => move(i, -1)}
                      aria-label={`Move ${itemName(item, i)} up`}>↑</button>
              <button type="button" className="btn" disabled={i === value.length - 1}
                      onClick={() => move(i, 1)} aria-label={`Move ${itemName(item, i)} down`}>↓</button>
              <button type="button" className="btn" onClick={() => remove(i)}
                      aria-label={`Remove ${itemName(item, i)}`}>Remove</button>
            </span>
          </div>
          <div className="pf-grid">
            {Object.entries(item).map(([kk, vv]) => (
              <Field key={kk} path={[...path, i, kk]} k={kk} value={vv} onChange={onChange} />
            ))}
          </div>
        </div>
      )) : (
        <div className="pf-values">
          {value.map((v, i) => (
            <span key={i} className="pf-value">
              <input value={v ?? ''} aria-label={`${label} ${i + 1}`}
                     onChange={(e) => onChange([...path, i], e.target.value)} />
              <button type="button" className="btn" onClick={() => remove(i)}
                      aria-label={`Remove ${label} ${i + 1}`}>×</button>
            </span>
          ))}
        </div>
      )}
      <button type="button" className="btn pf-add" onClick={add}>+ Add {records ? 'entry' : 'value'}</button>
    </div>
  )
}

function Field({ path, k, value, onChange }) {
  if (typeof k === 'string' && k.startsWith('_')) {
    // Notes / metadata — show as a caption, not editable, so we never mangle them.
    return <div className="note-caption">{typeof value === 'string' ? value : k}</div>
  }
  if (Array.isArray(value)) return <ListField path={path} k={k} value={value} onChange={onChange} />
  if (value && typeof value === 'object') {
    return (
      <div className="pf-subsection full">
        <div className="pf-subtitle">{titleize(k)}</div>
        <div className="pf-grid">
          {Object.entries(value).map(([kk, vv]) => (
            <Field key={kk} path={[...path, kk]} k={kk} value={vv} onChange={onChange} />
          ))}
        </div>
      </div>
    )
  }
  if (typeof value === 'boolean') {
    return (
      <label className="pf-field">
        <span>{titleize(k)}</span>
        <select value={String(value)} onChange={(e) => onChange(path, e.target.value === 'true')}>
          <option value="true">Yes</option>
          <option value="false">No</option>
        </select>
      </label>
    )
  }
  if (typeof value === 'number') {
    return (
      <label className="pf-field">
        <span>{titleize(k)}</span>
        <input type="number" value={value} onChange={(e) => onChange(path, Number(e.target.value))} />
      </label>
    )
  }
  // A string, or null for "not set" (work_history `end` is null while `current` is true).
  const text = value ?? ''
  const long = text.length > 60
  return (
    <label className={`pf-field ${long ? 'full' : ''}`}>
      <span>{titleize(k)}</span>
      {long
        ? <textarea rows={2} value={text} onChange={(e) => onChange(path, e.target.value)} />
        : <input value={text} onChange={(e) => onChange(path, e.target.value)} />}
    </label>
  )
}

function Section({ path, name, obj, onChange }) {
  return (
    <div className="card pf-section">
      <div className="section-label">{titleize(name)}</div>
      <div className="pf-grid">
        {Object.entries(obj).map(([k, v]) => (
          <Field key={k} path={[...path, name, k]} k={k} value={v} onChange={onChange} />
        ))}
      </div>
    </div>
  )
}

function DraftsToggle() {
  const notify = useToast()
  const [pref, setPref] = useState(null)   // { enabled, available, model }
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.draftsPref().then(setPref).catch((e) => notify(e.message, 'error'))
    /* eslint-disable-next-line */
  }, [])

  const toggle = async () => {
    if (!pref) return
    setBusy(true)
    try {
      const next = await api.setDraftsPref(!pref.enabled)
      setPref(next)
      notify(next.enabled ? `Claude drafting ON (${next.model})` : 'Claude drafting OFF', 'success')
    } catch (e) { notify(e.message, 'error', 6000) } finally { setBusy(false) }
  }

  if (!pref) return null
  return (
    <div className="card pf-section">
      <div className="section-label">Claude drafting</div>
      <div className="pf-grid">
        <label className="pf-field full">
          <span>Tier-2 answer drafting</span>
          <button className={`btn ${pref.enabled ? 'btn-primary' : ''}`} disabled={busy} onClick={toggle}>
            {busy ? '…' : pref.enabled ? `ON — model ${pref.model}` : 'OFF'}
          </button>
        </label>
      </div>
      <p className="page-hint">
        When ON, Claude drafts essay answers the bank hasn&apos;t seen (you still approve or edit each one before it&apos;s used).
        When OFF, only your saved answer-bank replies are offered and no Claude credits are spent. Submitting is never affected.
        {pref.enabled && !pref.available && ' ⚠️ No claude binary found, so nothing will actually draft.'}
      </p>
    </div>
  )
}

function AiPassToggle() {
  const notify = useToast()
  const [pref, setPref] = useState(null)   // { enabled, available, model }
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.aiPref().then(setPref).catch((e) => notify(e.message, 'error'))
    /* eslint-disable-next-line */
  }, [])

  const toggle = async () => {
    if (!pref) return
    setBusy(true)
    try {
      const next = await api.setAiPref(!pref.enabled)
      setPref(next)
      notify(next.enabled ? `AI fill + verify ON (${next.model})` : 'AI fill + verify OFF', 'success')
    } catch (e) { notify(e.message, 'error', 6000) } finally { setBusy(false) }
  }

  if (!pref) return null
  return (
    <div className="card pf-section">
      <div className="section-label">AI fill + verify</div>
      <div className="pf-grid">
        <label className="pf-field full">
          <span>One Claude pass per form</span>
          <button className={`btn ${pref.enabled ? 'btn-primary' : ''}`} disabled={busy} onClick={toggle}>
            {busy ? '…' : pref.enabled ? `ON — model ${pref.model}` : 'OFF'}
          </button>
        </label>
      </div>
      <p className="page-hint">
        When ON, one Claude call reads each whole form: it fills what the rules left blank and
        checks what they filled, flagging anything that looks mis-mapped. Salary-style
        questions are only ever suggested, never typed in, and every AI answer is listed at the
        top of your review before you approve. When OFF, deterministic fills only.
        {pref.enabled && !pref.available && ' ⚠️ No claude binary found, so nothing will actually run.'}
      </p>
    </div>
  )
}

export default function Profile() {
  const notify = useToast()
  const [profile, setProfile] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = async () => {
    try { setProfile(await api.profile()); setDirty(false) }
    catch (e) { notify(e.message, 'error') }
  }
  useEffect(() => { load() /* eslint-disable-next-line */ }, [])

  const onChange = (path, value) => { setProfile((p) => setAt(p, path, value)); setDirty(true) }

  const save = async () => {
    setSaving(true)
    try { await api.saveProfile(profile); setDirty(false); notify('Profile saved', 'success') }
    catch (e) { notify(e.message, 'error', 6000) } finally { setSaving(false) }
  }

  const rebuild = async () => {
    try {
      const r = await api.buildExtension()
      notify(r.ok ? 'Extension config rebuilt — reload it in chrome://extensions' : 'Build failed', r.ok ? 'success' : 'error', 6000)
    } catch (e) { notify(e.message, 'error', 6000) }
  }

  if (!profile) return <div className="empty">Loading…</div>

  return (
    <div className="profile">
      <div className="page-head">
        <h2>Profile</h2>
        <div className="head-actions">
          <button className="btn" onClick={rebuild} title="restage profile.json into the extension">↻ Rebuild extension</button>
          <button className="btn btn-primary" disabled={!dirty || saving} onClick={save}>{saving ? 'Saving…' : 'Save profile'}</button>
        </div>
      </div>
      <p className="page-hint">Deterministic autofill values. After saving, hit <b>Rebuild extension</b> and reload the unpacked extension for changes to take effect.</p>
      <DraftsToggle />
      <AiPassToggle />
      {Object.entries(profile).map(([name, val]) =>
        (val && typeof val === 'object' && !Array.isArray(val))
          ? <Section key={name} path={[]} name={name} obj={val} onChange={onChange} />
          : <div key={name} className="card pf-section"><div className="pf-grid"><Field path={[name]} k={name} value={val} onChange={onChange} /></div></div>
      )}
    </div>
  )
}
