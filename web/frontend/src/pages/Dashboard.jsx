import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'
import { Avatar, relDate, shortDate, useHotkeys } from '../ui.jsx'
import { BUCKETS, BUCKET_LABEL, TO_APPLY, inverseMove, isEditable } from '../piles.js'
import MoveMenu from '../MoveMenu.jsx'
import JobDrawer from './JobDrawer.jsx'
import Focus from './Focus.jsx'
import Kanban from './Kanban.jsx'

const VIEW_KEY = 'jobpipeline.view'
const VIEWS = [
  { id: 'focus', label: 'focus', icon: '◎', hint: 'one role at a time (1)' },
  { id: 'kanban', label: 'board', icon: '▦', hint: 'the whole pipeline as columns (2)' },
  { id: 'list', label: 'list', icon: '☰', hint: 'search and scan everything (3)' },
]

/** A remembered view name that no longer exists must not strand the console on a blank page. */
function storedView() {
  const s = localStorage.getItem(VIEW_KEY)
  return VIEWS.some((v) => v.id === s) ? s : 'focus'
}

function ViewSwitch({ view, onView }) {
  return (
    <div className="viewswitch" role="tablist" aria-label="view">
      {VIEWS.map((v) => (
        <button key={v.id} role="tab" aria-selected={view === v.id} title={v.hint}
          className={view === v.id ? 'on' : ''} onClick={() => onView(v.id)}>
          <span className="vs-icon">{v.icon}</span>{v.label}
        </button>
      ))}
    </div>
  )
}

function Row({ row, onOpen, onMove, leaving }) {
  const editable = isEditable(row.bucket)
  return (
    <div className={`row row-${row.bucket} ${leaving ? 'row-leaving' : ''}`}>
      <Avatar name={row.company} />
      <div className="row-main" onClick={() => editable && onOpen(row)} role={editable ? 'button' : undefined}>
        <div className="row-title">
          <span className="row-company">{row.company}</span>
          <span className="row-role">{row.title}</span>
          {row.tier === 'dream' && <span className="badge badge-dream">dream</span>}
        </div>
        <div className="row-sub">
          {row.location && <span className="row-loc">{row.location}</span>}
          <span className={`tag tag-${row.bucket}`}>{row.tag}</span>
          {row.resume && <span className="row-resume">{row.resume}</span>}
        </div>
      </div>
      <div className="row-date" title={row.date}>
        <span className="row-date-rel">{relDate(row.date) || '—'}</span>
        <span className="row-date-abs">{shortDate(row.date)}</span>
      </div>
      <div className="row-actions">
        {editable && <button className="btn btn-sm" onClick={() => onOpen(row)}>Open</button>}
        {row.url && row.apply_url && row.apply_url !== row.url &&
          <a className="btn btn-sm btn-ghost" href={row.url} target="_blank" rel="noreferrer"
            title="Job description">JD</a>}
        {row.url && <a className="btn btn-sm btn-ghost btn-icon" href={row.apply_url || row.url}
          target="_blank" rel="noreferrer"
          title={row.apply_url && row.apply_url !== row.url ? 'Application form' : 'Open'}>↗</a>}
        <MoveMenu row={row} onMove={onMove} />
      </div>
    </div>
  )
}

function StatStrip({ c, onPick }) {
  const toApply = c.to_apply || 0
  const sent = c.submitted || 0
  const decided = sent + (c.blocked || 0)
  const total = toApply + decided
  const pct = total ? Math.round((decided / total) * 100) : 0
  const stats = [
    { id: 'to_apply', n: toApply, label: 'to apply', tone: 'amber' },
    { id: 'filled', n: c.filled || 0, label: 'ready to send', tone: 'blue' },
    { id: 'submitted', n: sent, label: 'sent', tone: 'green' },
    { id: 'blocked', n: c.blocked || 0, label: 'blocked', tone: 'red' },
  ]
  return (
    <div className="statstrip">
      {stats.map((s) => (
        <button key={s.id} className={`stat stat-${s.tone}`} onClick={() => onPick(s.id)}>
          <span className="stat-n">{s.n}</span>
          <span className="stat-label">{s.label}</span>
        </button>
      ))}
      <div className="stat stat-meter">
        <div className="meter"><span style={{ width: `${pct}%` }} /></div>
        <span className="stat-label">{pct}% of everything discovered is decided</span>
      </div>
    </div>
  )
}

export default function Dashboard({ onChanged, bridgeAlive, refreshSignal }) {
  const notify = useToast()
  const [data, setData] = useState({ rows: [], counts: {} })
  const [view, setView] = useState(storedView)
  const [bucket, setBucket] = useState('to_apply')
  const [tier, setTier] = useState('all')
  const [q, setQ] = useState('')
  const [openKey, setOpenKey] = useState(null)
  const [loading, setLoading] = useState(true)
  const [leaving, setLeaving] = useState({})     // dedup_key -> true, for the exit animation
  const [optimistic, setOptimistic] = useState({}) // dedup_key -> bucket, while a move is in flight
  const [lastMove, setLastMove] = useState(null) // the one undoable move
  const undoTimer = useRef(null)

  useEffect(() => { localStorage.setItem(VIEW_KEY, view) }, [view])

  // `quiet` refetches without the loading flash, so a pile move doesn't blank the screen.
  const load = useCallback(async (quiet) => {
    if (!quiet) setLoading(true)
    try { setData(await api.dashboard(null)) }
    catch (e) { notify(e.message, 'error') }
    finally { if (!quiet) setLoading(false) }
  }, [notify])

  // Reload when a run-control action bumps the signal — WITHOUT remounting (which would
  // close an open job drawer mid-edit).
  useEffect(() => { load() }, [load, refreshSignal])

  // A dropped card has to land in its new column immediately or the ~200ms round trip reads
  // as a failed drop. The override is applied here, where every view reads its rows, so the
  // three can't disagree about which pile a role is in.
  const allRows = useMemo(() => (
    Object.keys(optimistic).length
      ? data.rows.map((r) => (optimistic[r.dedup_key] ? { ...r, bucket: optimistic[r.dedup_key] } : r))
      : data.rows
  ), [data.rows, optimistic])

  // Tier + search apply to both the list and the board; the pile chips are list-only (on a
  // board, filtering to one pile would just empty five columns).
  const found = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return allRows.filter((r) => {
      if (tier !== 'all' && r.tier !== tier) return false
      if (needle && !(`${r.company} ${r.title} ${r.location}`.toLowerCase().includes(needle))) return false
      return true
    })
  }, [allRows, tier, q])

  const rows = useMemo(() => {
    const want = BUCKETS.find((b) => b.id === bucket)?.of
    return found.filter((r) => (
      want ? want.includes(r.bucket) : bucket === 'all' || r.bucket === bucket))
  }, [found, bucket])

  // Focus mode always works the to-apply pile, whatever the list filter is set to.
  const focusRows = useMemo(
    () => allRows.filter((r) => TO_APPLY.includes(r.bucket)), [allRows])

  // No confirm dialogs and no success toast: a move is one keypress, always reversible, and
  // the row visibly leaves the pile — so the feedback IS the animation. Only failures speak.
  //
  // `optimistic` is the board's variant: instead of fading the card out where it stands, it
  // reappears in the destination column for the length of the request. A server refusal
  // (Ready still has open questions) clears it and the card snaps back — the right signal.
  const move = useCallback(async (row, m, { undoable = true, optimistic: optTo = null } = {}) => {
    if (optTo) setOptimistic((s) => ({ ...s, [row.dedup_key]: optTo }))
    else setLeaving((s) => ({ ...s, [row.dedup_key]: true }))
    try {
      const res = await api.setState(row.dedup_key, m.target, m.reason ?? null, {
        company: row.company, title: row.title, location: row.location, url: row.url,
      })
      if (undoable) {
        const inv = inverseMove(row.bucket)
        setLastMove({ row, label: m.label || BUCKET_LABEL[m.target], inv })
        clearTimeout(undoTimer.current)
        undoTimer.current = setTimeout(() => setLastMove(null), 12000)
      }
      if (res?.note) notify(res.note, 'info')
      await load(true); onChanged?.()
    } catch (e) {
      notify(`${row.company}: ${e.message}`, 'error', 6000)
    } finally {
      // Cleared only after load(true) resolved, so the refetch already carries the real
      // bucket and the card never flickers back through its old column.
      setLeaving((s) => { const { [row.dedup_key]: _, ...rest } = s; return rest })
      setOptimistic((s) => { const { [row.dedup_key]: _, ...rest } = s; return rest })
    }
  }, [load, notify, onChanged])

  const undo = useCallback(() => {
    if (!lastMove) return
    const { row, inv } = lastMove
    setLastMove(null)
    move({ ...row, bucket: 'undo' }, { target: inv.target, label: `back to ${inv.label}` },
      { undoable: false, optimistic: view === 'kanban' ? inv.target : null })
  }, [lastMove, move, view])

  // Global view switching. `L` stays bound inside focus mode for muscle memory; the digits
  // work everywhere, including from the board and the list.
  useHotkeys({ 1: () => setView('focus'), 2: () => setView('kanban'), 3: () => setView('list') },
    !openKey)

  const c = data.counts || {}

  if (loading) {
    return <div className="skeletons">{[0, 1, 2, 3, 4].map((i) => <div key={i} className="skel" />)}</div>
  }

  const drawer = openKey && (
    <JobDrawer dedupKey={openKey} bridgeAlive={bridgeAlive}
      onClose={() => setOpenKey(null)}
      onChanged={() => { load(true); onChanged?.() }} />
  )

  const switcher = <ViewSwitch view={view} onView={setView} />

  if (view === 'focus') {
    return (
      <div className="dashboard">
        <Focus rows={focusRows} lastMove={lastMove} undo={undo} hotkeys={!openKey}
          onOpen={(row) => setOpenKey(row.dedup_key)}
          onMove={move}
          switcher={switcher}
          onList={() => setView('list')} />
        {drawer}
      </div>
    )
  }

  if (view === 'kanban') {
    return (
      <div className="dashboard">
        <StatStrip c={c} onPick={(id) => { setBucket(id); setView('list') }} />
        <Kanban rows={found} lastMove={lastMove} undo={undo} switcher={switcher}
          onOpen={(row) => setOpenKey(row.dedup_key)}
          onMove={move} />
        {drawer}
      </div>
    )
  }

  return (
    <div className="dashboard">
      <StatStrip c={c} onPick={setBucket} />

      <div className="toolbar">
        <div className="chips">
          {BUCKETS.map((b) => (
            <button key={b.id}
              className={`chip chip-${b.id} ${bucket === b.id ? 'chip-active' : ''}`}
              onClick={() => setBucket(b.id)}>
              {b.label}
              <span className="chip-count">{b.id === 'all' ? c.all : c[b.id] ?? 0}</span>
            </button>
          ))}
        </div>
        {switcher}
      </div>

      <div className="toolbar">
        <div className="chips">
          {['all', 'pipeline', 'dream'].map((t) => (
            <button key={t}
              className={`chip chip-tier ${tier === t ? 'chip-active' : ''}`}
              onClick={() => setTier(t)}>
              {t === 'all' ? 'All tiers' : t}
              {t !== 'all' && <span className="chip-count">{c['tier_' + t]}</span>}
            </button>
          ))}
        </div>
        <input className="search" placeholder="Search company / role…"
          value={q} onChange={(e) => setQ(e.target.value)} />
      </div>

      {rows.length === 0
        ? <div className="empty">No roles in this view.</div>
        : (
          <div className="rows">
            {rows.map((r) => (
              <Row key={`${r.bucket}-${r.dedup_key}`} row={r} leaving={leaving[r.dedup_key]}
                onOpen={(row) => setOpenKey(row.dedup_key)} onMove={move} />
            ))}
          </div>
        )}

      {lastMove && (
        <div className="undobar">
          <span>Moved <strong>{lastMove.row.company}</strong> — {lastMove.label}</span>
          <button className="btn btn-sm" onClick={undo}>↩ Undo</button>
        </div>
      )}

      {drawer}
    </div>
  )
}
