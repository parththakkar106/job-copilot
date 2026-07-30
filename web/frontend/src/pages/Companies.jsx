import React, { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'

export default function Companies() {
  const notify = useToast()
  const [data, setData] = useState({ companies: [], counts: {} })
  const [tier, setTier] = useState('all')
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    try { setData(await api.companies()) }
    catch (e) { notify(e.message, 'error') } finally { setLoading(false) }
  }
  useEffect(() => { load() /* eslint-disable-next-line */ }, [])

  const list = useMemo(() => {
    const n = q.trim().toLowerCase()
    return data.companies.filter((c) =>
      (tier === 'all' || c.tier === tier) &&
      (!n || c.name.toLowerCase().includes(n)))
  }, [data.companies, tier, q])

  const toggle = async (c) => {
    const next = c.tier === 'dream' ? 'pipeline' : 'dream'
    try {
      await api.setTier(c.name, next)
      notify(`${c.name} → ${next}`, 'success')
      setData((d) => ({
        ...d,
        companies: d.companies.map((x) => x.name === c.name ? { ...x, tier: next } : x),
        counts: {
          ...d.counts,
          pipeline: d.counts.pipeline + (next === 'pipeline' ? 1 : -1),
          dream: d.counts.dream + (next === 'dream' ? 1 : -1),
        },
      }))
    } catch (e) { notify(e.message, 'error', 6000) }
  }

  const c = data.counts || {}

  return (
    <div className="companies">
      <div className="page-head">
        <h2>Companies</h2>
        <div className="head-actions">
          <input className="search" placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>
      <p className="page-hint">Only <b>pipeline</b> companies are auto-applied. <b>Dream</b> roles are excluded from the loop (manual + referral). Toggling a tier edits companies.yaml in place.</p>

      <div className="chips">
        {[['all', `All (${c.total || 0})`], ['pipeline', `Pipeline (${c.pipeline || 0})`], ['dream', `Dream (${c.dream || 0})`]].map(([id, label]) => (
          <button key={id} className={`chip ${tier === id ? 'chip-active' : ''}`} onClick={() => setTier(id)}>{label}</button>
        ))}
      </div>

      {loading ? <div className="empty">Loading…</div> : (
        <div className="co-grid">
          {list.map((co) => (
            <div key={co.name} className={`co-card co-${co.tier}`}>
              <div className="co-name">{co.name}</div>
              <div className="co-meta"><span className="tag-mini">{co.ats}</span><span className="mono">{co.slug}</span></div>
              <button className={`co-tier badge badge-${co.tier === 'dream' ? 'dream' : 'active'}`} onClick={() => toggle(co)} title="click to flip tier">
                {co.tier}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
