import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'

// Each career path is also a resume track, so the page names the resumes a pick implies.
export default function Search() {
  const notify = useToast()
  const [data, setData] = useState(null)
  const [chosen, setChosen] = useState(new Set())
  const [levels, setLevels] = useState(new Set())
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    api.search()
      .then((d) => {
        setData(d)
        setChosen(new Set(d.paths.filter((p) => p.selected).map((p) => p.key)))
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
  const label = Object.fromEntries(data.paths.map((p) => [p.key, p.label]))
  const picked = data.paths.filter((p) => chosen.has(p.key))

  const save = async () => {
    setSaving(true)
    try {
      const r = await api.saveSearch([...chosen], [...levels])
      // The extension carries its own copy of the profile, and its resume tracks just changed.
      await api.buildExtension().catch(() => {})
      notify(`Saved. Resume tracks: ${r.tracks.map((t) => label[t] || t).join(', ')}. ` +
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
        Pick the career paths and experience levels you want. The daily run keeps a role only
        if its title matches a keyword of a path you picked, at a level you picked. You upload
        one resume for each path. Edit the keywords in <code>data/filters.yaml</code>.
      </p>

      <fieldset className="card search-group">
        <legend>Career paths</legend>
        {data.paths.map((p) => (
          <label key={p.key} className="search-opt search-path">
            <input type="checkbox" checked={chosen.has(p.key)}
                   onChange={() => toggle(chosen, setChosen, p.key)} />
            <span>
              {p.label}
              <span className="search-kw">{p.keywords.join(', ')}</span>
            </span>
          </label>
        ))}
      </fieldset>

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
          {picked.length
            ? <>Resumes to upload on the Setup page: <b>{picked.map((p) => p.label).join(', ')}</b>.</>
            : 'Pick at least one career path.'}
        </p>
        <button className="btn btn-primary" onClick={save}
                disabled={saving || !chosen.size || !levels.size}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  )
}
