import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'

// Each topic's field is also a resume track, so the page names the resumes a pick implies.
const FIELD_LABELS = { quant: 'Quant', ai: 'AI and ML', ds: 'Data', tech: 'Software' }
const fieldLabel = (f) => FIELD_LABELS[f] || f

export default function Search() {
  const notify = useToast()
  const [data, setData] = useState(null)
  const [topics, setTopics] = useState(new Set())
  const [levels, setLevels] = useState(new Set())
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    api.search()
      .then((d) => {
        setData(d)
        setTopics(new Set(d.topics.filter((t) => t.selected).map((t) => t.key)))
        setLevels(new Set(d.levels.filter((l) => l.selected).map((l) => l.key)))
      })
      .catch((e) => notify(e.message, 'error'))
  }, [notify])

  if (!data) return <div className="card"><p className="muted">Loading the search options…</p></div>

  const toggle = (set, setter, key) => {
    const next = new Set(set)
    next.has(key) ? next.delete(key) : next.add(key)
    setter(next)
  }
  const groups = [...new Set(data.topics.map((t) => t.field))]
  const tracks = groups.filter((f) => data.topics.some((t) => t.field === f && topics.has(t.key)))

  const save = async () => {
    setSaving(true)
    try {
      const r = await api.saveSearch([...topics], [...levels])
      // The extension carries its own copy of the profile, and its resume tracks just changed.
      await api.buildExtension().catch(() => {})
      notify(`Saved. Resume tracks: ${r.tracks.map(fieldLabel).join(', ')}. ` +
             'Refresh discovery to search with it.', 'success', 6000)
    } catch (e) {
      notify(e.message, 'error', 6000)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="search-page">
      <div className="page-head"><h2>What to search</h2></div>
      <p className="page-hint">
        Pick the topics and experience levels you want. The daily run keeps a role only if its
        title matches a topic and a level you picked.
      </p>

      {groups.map((f) => (
        <fieldset key={f} className="card search-group">
          <legend>{fieldLabel(f)}</legend>
          {data.topics.filter((t) => t.field === f).map((t) => (
            <label key={t.key} className="search-opt">
              <input type="checkbox" checked={topics.has(t.key)}
                     onChange={() => toggle(topics, setTopics, t.key)} />
              {t.label}
            </label>
          ))}
        </fieldset>
      ))}

      <fieldset className="card search-group">
        <legend>Experience level</legend>
        {data.levels.map((l) => (
          <label key={l.key} className="search-opt">
            <input type="checkbox" checked={levels.has(l.key)}
                   onChange={() => toggle(levels, setLevels, l.key)} />
            {l.label}
          </label>
        ))}
      </fieldset>

      <div className="card search-foot">
        <p>
          {tracks.length
            ? <>Resumes to upload on the Setup page: <b>{tracks.map(fieldLabel).join(', ')}</b>.</>
            : 'Pick at least one topic.'}
        </p>
        <button className="btn btn-primary" onClick={save}
                disabled={saving || !topics.size || !levels.size}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  )
}
