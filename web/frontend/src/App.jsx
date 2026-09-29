import React, { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import { ToastProvider, useToast } from './toast.jsx'
import Dashboard from './pages/Dashboard.jsx'
import AnswerBank from './pages/AnswerBank.jsx'
import Profile from './pages/Profile.jsx'
import Companies from './pages/Companies.jsx'
import Setup from './pages/Setup.jsx'
import Search from './pages/Search.jsx'

const TABS = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'bank', label: 'Answer bank' },
  { id: 'profile', label: 'Profile' },
  { id: 'companies', label: 'Companies' },
  { id: 'search', label: 'Search' },
]

// Setup is not in TABS: once you are set up it is clutter, so it only appears as a tab
// while it still has something to say. A configured install never sees it.
const SETUP_TAB = { id: 'setup', label: 'Setup' }

function RunControls({ onChanged }) {
  const notify = useToast()
  const [busy, setBusy] = useState('')

  const run = async (name, fn, ok) => {
    setBusy(name)
    try {
      const r = await fn()
      notify(ok(r), 'success')
      onChanged?.()
    } catch (e) {
      notify(e.message, 'error', 6000)
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="runbar">
      <button className="btn" disabled={!!busy}
        onClick={() => run('scraper', api.runScraper,
          (r) => `Discovery refreshed — ${r.queue_size} roles in the queue`)}>
        {busy === 'scraper' ? 'Refreshing…' : '↻ Refresh discovery'}
      </button>
      <button className="btn" disabled={!!busy}
        onClick={() => run('collect', api.runCollect,
          () => 'Collection run requested — watch Chrome + Telegram')}>
        {busy === 'collect' ? 'Starting…' : '▶ Collect queue'}
      </button>
      <button className="btn btn-warn" disabled={!!busy}
        onClick={() => {
          if (!confirm('Send every approved application now? This submits them for real.')) return
          run('flush', api.runFlush, (r) => `Sending ${r.approved_now} approved application(s)`)
        }}>
        {busy === 'flush' ? 'Sending…' : '⇪ Send approved'}
      </button>
    </div>
  )
}

function Shell() {
  const [tab, setTab] = useState('dashboard')
  const [status, setStatus] = useState({ bridge_alive: false, counts: {} })
  const [refreshKey, setRefreshKey] = useState(0)
  // Decided once at mount and never revoked during the session. If it flipped off the
  // moment the last required step passed, the tab would vanish from under someone standing
  // on it and render a blank page. It simply does not come back after a reload.
  const [showSetup, setShowSetup] = useState(false)
  const bump = useCallback(() => setRefreshKey((k) => k + 1), [])

  const loadStatus = useCallback(async () => {
    try { setStatus(await api.status()) } catch { /* backend down; ignore */ }
  }, [])

  // Asked once on mount. A fresh install opens ON the checklist rather than on an empty
  // dashboard that silently reads the shipped example config.
  useEffect(() => {
    let cancelled = false
    api.setupStatus()
      .then((d) => {
        if (cancelled) return
        // CORE steps only. Counting the optional extras here kept the Setup tab pinned to
        // the nav forever for anyone who reasonably never wants a Telegram bot or a mail
        // credential - an opt-out that keeps nagging is not an opt-out.
        const core = d.core_total ?? d.total
        const incomplete = !d.ready || (d.core_done ?? d.done_count) < core
        setShowSetup(incomplete)
        if (!d.ready) setTab('setup')   // only *required* work forces you here
      })
      .catch(() => {})                  // never block the console on a failed probe
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    loadStatus()
    const t = setInterval(loadStatus, 8000)
    return () => clearInterval(t)
  }, [loadStatus, refreshKey])

  const tabs = showSetup ? [SETUP_TAB, ...TABS] : TABS

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">💼</span>
          <span className="brand-name">Job Pipeline</span>
        </div>
        <nav className="tabs">
          {tabs.map((t) => (
            <button key={t.id}
              className={`tab ${tab === t.id ? 'tab-active' : ''}`}
              onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </nav>
        <div className="topbar-right">
          <span className={`bridge-pill ${status.bridge_alive ? 'up' : 'down'}`}>
            <span className="dot" /> bridge {status.bridge_alive ? 'up' : 'down'}
          </span>
        </div>
      </header>

      <RunControls onChanged={bump} />

      <main className="content">
        {tab === 'dashboard' && <Dashboard refreshSignal={refreshKey} onChanged={bump} bridgeAlive={status.bridge_alive} />}
        {tab === 'bank' && <AnswerBank />}
        {tab === 'profile' && <Profile />}
        {tab === 'companies' && <Companies />}
        {tab === 'search' && <Search />}
        {tab === 'setup' && (
          <Setup onGo={(link) => setTab(link.replace('/', ''))} />
        )}
      </main>
    </div>
  )
}

export default function App() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  )
}
