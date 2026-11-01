import React, { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'
import JobDrawer from './JobDrawer.jsx'

// What each queued link's status means, in the order a link moves through them.
const LINK_STATUS = {
  waiting: 'Waiting for Chrome',
  running: 'Filling',
  filled: 'Filled',
  needs_answers: 'Needs your answers',
  ready: 'Ready to approve',
  sending: 'Sending',
  sent: 'Sent',
  held: 'Needs you',
  skipped: 'Skipped',
  expired: 'Expired',
  failed: 'Failed',
}

const today = () => {
  const d = new Date()
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset())   // the local day, not the UTC day
  return d.toISOString().slice(0, 10)
}

function Field({ label, children }) {
  return <label className="ap-field"><span>{label}</span>{children}</label>
}

export default function Applied({ bridgeAlive }) {
  const notify = useToast()
  const [openKey, setOpenKey] = useState(null)
  const [rows, setRows] = useState([])
  const [links, setLinks] = useState([])
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState('')
  const [draft, setDraft] = useState(null)   // the editable preview of a pasted link

  const loadRows = async () => {
    try { setRows((await api.applied()).rows) }
    catch (e) { notify(e.message, 'error') } finally { setLoading(false) }
  }
  const loadLinks = async () => {
    try { setLinks((await api.links()).links) } catch { /* the next poll retries */ }
  }
  useEffect(() => {
    loadRows()
    loadLinks()
    // The bridge updates queued links in its own process, so only a poll can see them.
    const t = setInterval(loadLinks, 4000)
    return () => clearInterval(t)
    // eslint-disable-next-line
  }, [])

  const list = useMemo(() => {
    const n = q.trim().toLowerCase()
    return rows.filter((r) => !n || `${r.company} ${r.title} ${r.location}`.toLowerCase().includes(n))
  }, [rows, q])

  const act = async (name, fn) => {
    setBusy(name)
    try { await fn() } catch (e) { notify(e.message, 'error', 6000) } finally { setBusy('') }
  }

  const read = () => act('read', async () => {
    const r = await api.readLink(url.trim())
    if (r.already_applied) notify('That role is already in your applied list', 'info', 6000)
    setDraft({ company: r.company, title: r.title, location: r.location, url: r.url, applied: today() })
  })

  const save = (e) => {
    e.preventDefault()
    act('save', async () => {
      await api.addApplied(draft)
      notify(`Logged ${draft.company}`, 'success')
      setDraft(null)
      setUrl('')
      await loadRows()
    })
  }

  const queue = () => act('queue', async () => {
    await api.queueLink(url.trim())
    notify('Queued. Watch its status below.', 'success')
    setUrl('')
    await loadLinks()
  })

  const remove = (link) => act('remove', async () => {
    await api.removeLink(link.id)
    await loadLinks()
  })

  const approve = (link) => {
    if (!confirm(`Submit ${link.company || 'this role'} for real? This also sends any other approved roles.`)) return
    act('approve', async () => {
      await api.submitJob(link.dedup_key)
      await loadLinks()
    })
  }

  const set = (k) => (e) => setDraft((d) => ({ ...d, [k]: e.target.value }))
  const hasUrl = !!url.trim()

  return (
    <div className="applied">
      {openKey && (
        <JobDrawer dedupKey={openKey} bridgeAlive={bridgeAlive}
          onClose={() => setOpenKey(null)} onChanged={loadLinks} />
      )}
      <div className="page-head">
        <h2>Applied ({rows.length})</h2>
        <div className="head-actions">
          <input className="search" placeholder="Search…" aria-label="Search applied roles"
            value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>
      <p className="page-hint">Paste a job link. Log a role you applied to yourself, or queue it
        so the pipeline fills it. A confident fill goes out on its own, and anything else waits
        for you here.</p>

      <div className="card ap-add">
        <input className="ap-url" type="url" placeholder="https://…" aria-label="Job link"
          value={url} onChange={(e) => setUrl(e.target.value)} />
        <button className="btn btn-primary" disabled={!hasUrl || !!busy} onClick={read}>
          {busy === 'read' ? 'Reading…' : 'I applied to this'}
        </button>
        <button className="btn" disabled={!hasUrl || !!busy} onClick={queue}>
          {busy === 'queue' ? 'Queueing…' : 'Fill it for me'}
        </button>
      </div>

      {draft && (
        <form className="card ap-draft" onSubmit={save}>
          <Field label="Company"><input required maxLength={200} value={draft.company} onChange={set('company')} /></Field>
          <Field label="Role"><input required maxLength={300} value={draft.title} onChange={set('title')} /></Field>
          <Field label="Location"><input maxLength={200} value={draft.location} onChange={set('location')} /></Field>
          <Field label="Date applied"><input type="date" required max={today()} value={draft.applied} onChange={set('applied')} /></Field>
          <div className="ap-actions">
            <button className="btn btn-primary" disabled={!!busy}>{busy === 'save' ? 'Saving…' : 'Save'}</button>
            <button type="button" className="btn btn-ghost" onClick={() => setDraft(null)}>Cancel</button>
          </div>
        </form>
      )}

      {links.length > 0 && (
        <div className="card">
          <h3 className="ap-sub">Queued links</h3>
          {links.map((l) => (
            <div className="ap-link" key={l.id}>
              <span className={`badge ${l.status === 'failed' ? 'ap-failed' : ''}`}>{LINK_STATUS[l.status] || l.status}</span>
              <a href={l.url} target="_blank" rel="noreferrer">{l.company ? `${l.company} · ${l.title}` : l.url}</a>
              {l.note && <span className="muted">{l.note}</span>}
              {l.status === 'needs_answers' && (
                <button className="btn btn-sm btn-primary" onClick={() => setOpenKey(l.dedup_key)}>
                  Answer ({l.unanswered})
                </button>
              )}
              {l.status === 'ready' && (
                <>
                  <button className="btn btn-sm btn-go" disabled={!!busy || !bridgeAlive}
                    title={bridgeAlive ? '' : 'bridge is down'} onClick={() => approve(l)}>
                    Approve and send
                  </button>
                  <button className="btn btn-sm" onClick={() => setOpenKey(l.dedup_key)}>Review</button>
                </>
              )}
              {l.status === 'held' && (
                <button className="btn btn-sm" onClick={() => setOpenKey(l.dedup_key)}>Review</button>
              )}
              <button className="btn btn-sm btn-ghost" disabled={!!busy} onClick={() => remove(l)}>Remove</button>
            </div>
          ))}
        </div>
      )}

      {loading ? <div className="empty">Loading…</div> : list.length === 0 ? (
        <div className="empty">{rows.length ? 'No roles match that search.' : 'No applications yet.'}</div>
      ) : (
        <div className="ap-scroll">
          <table className="ap-table">
            <thead>
              <tr><th scope="col">Applied</th><th scope="col">Company</th><th scope="col">Role</th>
                <th scope="col" className="ap-loc">Location</th></tr>
            </thead>
            <tbody>
              {list.map((r) => (
                <tr key={r.dedup_key}>
                  <td className="mono">{r.applied}</td>
                  <td>{r.company}</td>
                  <td>{r.url ? <a href={r.url} target="_blank" rel="noreferrer">{r.title}</a> : r.title}</td>
                  <td className="ap-loc">{r.location}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
