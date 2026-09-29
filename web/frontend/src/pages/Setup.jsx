import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'

// First-run checklist. Every step here is something the console can already do on another
// tab, so this page is a router into existing UI rather than a second way of editing
// things — the setup pain was never a missing feature, it was that nothing told you which
// order to do them in or when you were finished.
//
// Steps flagged `manual` cannot be automated at all (Chrome requires a human to load an
// unpacked extension; BotFather is a conversation). They are shown rather than hidden, with
// a re-check button, because a checklist that quietly skips the hard parts is what made the
// README feel like a wall of text.
//
// The list is in two groups, and the split is the point. The core steps lead to a filled
// application, with the extension second because the extension is the product. The optional
// extras (Telegram, emailed verification codes) are control-plane conveniences the fill
// engine never needs, so they sit below their own heading and are left OUT of the progress
// bar - a permanent "4 of 6" would read as unfinished to someone who simply does not want
// a Telegram bot.

// One upload button per resume track. Each upload also rebuilds the extension, so its
// fallback copy of the PDFs matches the cache.
function ResumeUploads({ tracks, onDone }) {
  const notify = useToast()
  const [uploading, setUploading] = useState('')

  const upload = async (tag, file) => {
    if (!file) return
    setUploading(tag)
    try {
      await api.uploadResume(tag, file)
      await api.buildExtension().catch(() => {})
      notify(`Saved the ${tag} resume.`, 'success')
      onDone()
    } catch (e) {
      notify(`Could not save the ${tag} resume: ${e.message}`, 'error')
    } finally {
      setUploading('')
    }
  }

  return (
    <ul className="setup-resumes">
      {tracks.map(({ tag, label, cached, synced }) => (
        <li key={tag}>
          <label>
            <b>{label || tag}</b>{' '}
            <span className="muted">{cached ? `saved${synced ? ` ${synced}` : ''}` : 'no PDF yet'}</span>{' '}
            <input type="file" accept="application/pdf" disabled={!!uploading}
                   aria-label={`Upload the ${label || tag} resume PDF`}
                   onChange={(e) => { upload(tag, e.target.files[0]); e.target.value = '' }} />
          </label>
          {uploading === tag && <span className="muted"> Uploading…</span>}
        </li>
      ))}
    </ul>
  )
}

// Copy buttons for loading the extension. Chrome does not let a web page open a chrome://
// address, so the page copies it for you to paste into the address bar.
function ExtensionLoad({ path }) {
  const notify = useToast()
  const copy = async (text, what) => {
    try {
      await navigator.clipboard.writeText(text)
      notify(`Copied ${what}.`, 'success')
    } catch {
      notify(`Could not copy. Select it and copy it by hand: ${text}`, 'error')
    }
  }
  return (
    <ol className="setup-ext">
      <li>
        Open <code>chrome://extensions</code>{' '}
        <button className="btn" onClick={() => copy('chrome://extensions', 'the address')}>Copy address</button>
      </li>
      <li>Turn on <b>Developer mode</b>, then click <b>Load unpacked</b>.</li>
      <li>
        Select <code>{path}</code>{' '}
        <button className="btn" onClick={() => copy(path, 'the folder path')}>Copy path</button>
      </li>
    </ol>
  )
}

function Step({ step, index, onGo, onRecheck, onRefresh, busy }) {
  const { id, title, detail, done, manual, optional, unfilled, link } = step
  return (
    <li className={`setup-step ${done ? 'is-done' : ''}`}>
      <div className="setup-mark" aria-hidden="true">{done ? '✓' : index + 1}</div>
      <div className="setup-body">
        <div className="setup-head">
          <h3>{title}</h3>
          {manual && <span className="setup-tag">needs you</span>}
          {optional && <span className="setup-tag">optional</span>}
          {step.blocking && !step.minimum_met && <span className="setup-tag req">required</span>}
        </div>
        <p className="setup-detail">{detail}</p>

        {/* The unfilled list is the single most useful thing on this page: it turns "fill in
            your profile" into a finite, countable list of what is actually left. */}
        {unfilled?.length > 0 && (
          <details className="setup-holes">
            <summary>{unfilled.length} field{unfilled.length === 1 ? '' : 's'} still unset</summary>
            <ul>{unfilled.map((f) => <li key={f}><code>{f}</code></li>)}</ul>
          </details>
        )}

        {step.tracks && <ResumeUploads tracks={step.tracks} onDone={onRefresh} />}
        {step.path && <ExtensionLoad path={step.path} />}

        <div className="setup-actions">
          {link && !done && (
            <button className="btn btn-primary" onClick={() => onGo(link)}>
              {id === 'profile' ? 'Fill this in' : 'Open'}
            </button>
          )}
          {manual && (
            <button className="btn" disabled={busy} onClick={onRecheck}>
              {busy ? 'Checking…' : 'I did this, re-check'}
            </button>
          )}
        </div>
      </div>
    </li>
  )
}

export default function Setup({ onGo }) {
  const notify = useToast()
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async (announce) => {
    setBusy(true)
    try {
      const d = await api.setupStatus()
      setData(d)
      setErr('')
      if (announce) notify(d.ready ? 'Setup looks complete.' : 'Re-checked.', d.ready ? 'success' : 'info')
    } catch (e) {
      setErr(e.message || 'could not reach the backend')
    } finally {
      setBusy(false)
    }
  }, [notify])

  useEffect(() => { load(false) }, [load])

  if (err) return <div className="card"><p className="muted">Setup status unavailable: {err}</p></div>
  if (!data) return <div className="card"><p className="muted">Checking your setup…</p></div>

  const core = data.steps.filter((s) => !s.optional)
  const extras = data.steps.filter((s) => s.optional)
  const coreTotal = data.core_total ?? core.length
  const coreDone = data.core_done ?? core.filter((s) => s.done).length
  const pct = Math.round((coreDone / Math.max(coreTotal, 1)) * 100)

  return (
    <div className="setup">
      <div className="setup-intro card">
        <h2>Let's get you set up</h2>
        <p className="muted">
          {coreTotal} steps to a filled application, and only the first is required. The
          Chrome extension is the part that does the work — the rest of this list is about
          feeding it. Everything here is editable later.
        </p>
        <div className="setup-bar"><div className="setup-bar-fill" style={{ width: `${pct}%` }} /></div>
        <p className="setup-count">{coreDone} of {coreTotal} done</p>
        {data.ready && (
          <p className="setup-ready">
            The essentials are in place — you can start using the console whenever you like.
          </p>
        )}
      </div>

      <ol className="setup-list">
        {core.map((s, i) => (
          <Step key={s.id} step={s} index={i} onGo={onGo}
                onRecheck={() => load(true)} onRefresh={() => load(false)} busy={busy} />
        ))}
      </ol>

      {extras.length > 0 && (
        <div className="setup-extras">
          <h3 className="setup-extras-head">Optional extras</h3>
          <p className="muted setup-extras-note">
            The extension fills and submits without either of these, and it did for months
            before they existed. Add one if you want it; skipping them costs you a little
            typing and nothing else.
          </p>
          <ol className="setup-list">
            {extras.map((s, i) => (
              <Step key={s.id} step={s} index={coreTotal + i} onGo={onGo}
                    onRecheck={() => load(true)} onRefresh={() => load(false)} busy={busy} />
            ))}
          </ol>
        </div>
      )}

      <div className="card setup-foot">
        <button className="btn" disabled={busy} onClick={() => load(true)}>
          {busy ? 'Checking…' : 'Re-check everything'}
        </button>
        <span className="muted">
          Your details are saved to <code>data/profile.json</code>, which is gitignored — filling
          this in cannot commit anything personal.
        </span>
      </div>
    </div>
  )
}
