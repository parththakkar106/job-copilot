import React, { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import { ToastProvider, useToast } from './toast.jsx'
import Dashboard from './pages/Dashboard.jsx'
import AnswerBank from './pages/AnswerBank.jsx'
import Profile from './pages/Profile.jsx'
import Companies from './pages/Companies.jsx'

const TABS = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'bank', label: 'Answer bank' },
  { id: 'profile', label: 'Profile' },
  { id: 'companies', label: 'Companies' },
]

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
  const bump = useCallback(() => setRefreshKey((k) => k + 1), [])

  const loadStatus = useCallback(async () => {
    try { setStatus(await api.status()) } catch { /* backend down; ignore */ }
  }, [])

  useEffect(() => {
    loadStatus()
    const t = setInterval(loadStatus, 8000)
    return () => clearInterval(t)
  }, [loadStatus, refreshKey])

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">💼</span>
          <span className="brand-name">Job Pipeline</span>
        </div>
        <nav className="tabs">
          {TABS.map((t) => (
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
