import React, { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast.jsx'
import { Avatar, relDate, shortDate, useHotkeys } from '../ui.jsx'
import { BUCKET_LABEL, MOVES, isEditable } from '../piles.js'
import { QuestionList, withDrafts } from './JobDrawer.jsx'

// One role at a time, keyboard-first. The default view: the whole point is to burn through
// the to-apply pile without reading a table, so everything is reachable with one key and
// every action advances automatically.

const KEYS = [
  ['J / →', 'next'],
  ['K / ←', 'back'],
  ['↵', 'open the full record'],
  ['Ctrl+↵', 'save answers, approve, next'],
  ['A', 'approve (saves typed answers first)'],
  ['D', 'use all drafts'],
  ['S', 'skip'],
  ['M', 'applied by hand'],
  ['B', 'not interested'],
  ['O', 'open the form'],
  ['U', 'undo'],
  ['L', 'list view'],
  ['1 / 2 / 3', 'focus / board / list'],
]

function stateLine(row) {
  if (row.bucket === 'discovered') return { kind: 'wait', text: 'not collected yet — run Collect queue to fill it' }
  if (row.bucket === 'dream') return { kind: 'wait', text: 'dream role — apply by hand, then mark it' }
  if (row.bucket === 'manual') return { kind: 'wait', text: "no adapter for this board — fill it yourself, then mark it" }
  if (row.bucket === 'filled') {
    return row.tag === 'ready to submit'
      ? { kind: 'go', text: 'filled and ready to submit' }
      : { kind: 'warn', text: row.tag }
  }
  if (row.unanswered > 0) {
    const one = row.unanswered === 1
    return { kind: 'warn', text: `${row.unanswered} question${one ? '' : 's'} ${one ? 'needs' : 'need'} you` }
  }
  return { kind: 'go', text: row.tag || 'ready to approve' }
}

/**
 * The open questions of the role on the card, answerable in place, so approving a role
 * needs no drawer. Drafts start filled in. An empty box stays an open question rather than
 * a blank answer: nearly every escalated question is required, and a blank one would be
 * sent to the employer as is.
 */
function useInlineAnswers(row) {
  const key = row && row.bucket === 'in_queue' ? row.dedup_key : null
  const [rec, setRec] = useState(null)
  const [answers, setAnswers] = useState({})
  useEffect(() => {
    setRec(null); setAnswers({})
    if (!key) return
    let live = true
    api.job(key).then((r) => {
      if (!live) return
      const qs = (r.questions || []).filter((q) => q.answer == null)
      setRec({ ...r, questions: qs })
      setAnswers(withDrafts(qs, {}))
    }).catch(() => {})
    return () => { live = false }
  }, [key])
  const typed = rec ? rec.questions
    .filter((q) => String(answers[q.id] ?? '').trim() !== '')
    .map((q) => ({ id: q.id, value: answers[q.id] })) : []
  return { rec, answers, setAnswers, typed }
}

export default function Focus({ rows, onOpen, onMove, onList, undo, lastMove, switcher, hotkeys = true }) {
  const notify = useToast()
  const [i, setI] = useState(0)
  const [help, setHelp] = useState(false)
  const [busy, setBusy] = useState(false)

  // Rows shrink as roles get moved out of the pile. Clamp rather than reset so the cursor
  // lands on whatever slid into the slot — that's the next role, which is where you want it.
  const n = rows.length
  const idx = Math.min(i, Math.max(0, n - 1))
  useEffect(() => { if (i !== idx) setI(idx) }, [i, idx])

  const row = rows[idx]
  const moves = useMemo(() => (row ? MOVES[row.bucket] || [] : []), [row])
  const inline = useInlineAnswers(row)

  const go = (d) => setI((x) => Math.min(Math.max(0, x + d), Math.max(0, n - 1)))
  const hit = (target) => { if (row) onMove(row, { target }) }

  // Approving from the card saves what was typed on it first, then moves on to the next
  // role. If questions are still open the role stays put and says how many.
  const approve = async () => {
    if (!row || busy) return
    if (!inline.rec) { hit('filled'); return }
    setBusy(true)
    try {
      const r = inline.typed.length ? await api.saveAnswers(row.dedup_key, inline.typed) : inline.rec
      if (!r._complete) {
        const left = (r.questions || []).filter((q) => q.answer == null).length
        notify(`${row.company}: ${left} question${left === 1 ? '' : 's'} still open`, 'error')
        return
      }
      await onMove(row, { target: 'filled' })
      go(1)
    } catch (e) {
      notify(`${row.company}: ${e.message}`, 'error', 6000)
    } finally { setBusy(false) }
  }
  const byHot = (k) => moves.find((m) => m.hot === k)

  useHotkeys({
    j: () => go(1), arrowright: () => go(1), ' ': () => go(1),
    k: () => go(-1), arrowleft: () => go(-1),
    arrowdown: () => go(1), arrowup: () => go(-1),
    enter: () => row && isEditable(row.bucket) && onOpen(row),
    a: () => byHot('a') && approve(),
    d: () => inline.rec && inline.setAnswers(withDrafts(inline.rec.questions, inline.answers)),
    s: () => byHot('s') && hit('skipped'),
    m: () => byHot('m') && hit('submitted'),
    b: () => byHot('b') && hit('blocked'),
    o: () => row?.url && window.open(row.apply_url || row.url, '_blank', 'noreferrer'),
    u: () => undo(),
    l: onList,
    '?': () => setHelp((h) => !h),
    escape: () => setHelp(false),
    // Disabled while the drawer is open, or B/S would act on the role behind it.
  }, hotkeys)

  if (!n) {
    return (
      <div className="focus focus-clear">
        <div className="clear-mark">✓</div>
        <h2>Nothing left to apply to</h2>
        <p className="hint">
          Every discovered role is filled, sent or blocked. Refresh discovery to pull new ones,
          or <button className="linky" onClick={onList}>browse everything</button>.
        </p>
        {lastMove && <button className="btn" onClick={undo}>↩ Undo “{lastMove.label}”</button>}
      </div>
    )
  }

  const st = stateLine(row)
  const primary = moves.find((m) => m.primary)
  const rest = moves.filter((m) => !m.primary)

  return (
    <div className="focus">
      <div className="focus-top">
        <div className="focus-count">
          <strong>{idx + 1}</strong> of {n} to apply
        </div>
        <div className="focus-bar" role="progressbar" aria-valuenow={idx + 1} aria-valuemax={n}>
          <span style={{ width: `${((idx + 1) / n) * 100}%` }} />
        </div>
        <div className="focus-top-right">
          <button className="btn btn-sm btn-ghost" onClick={() => setHelp((h) => !h)}
            title="keyboard shortcuts">⌨ keys</button>
          {switcher}
        </div>
      </div>

      <div className="focus-stage">
        <button className="focus-arrow" onClick={() => go(-1)} disabled={idx === 0}
          aria-label="previous role">‹</button>

        <article className={`focus-card focus-${row.bucket}`} key={row.dedup_key}>
          <div className="focus-head">
            <Avatar name={row.company} size="lg" />
            <div className="focus-titles">
              <div className="focus-company">
                {row.company}
                {row.tier === 'dream' && <span className="badge badge-dream">dream</span>}
              </div>
              <h2 className="focus-role">{row.title}</h2>
              <div className="focus-meta">
                {row.location && <span>{row.location}</span>}
                {row.date && <span title={row.date}>posted {relDate(row.date)}</span>}
                <span className={`tag tag-${row.bucket}`}>{BUCKET_LABEL[row.bucket]}</span>
                {row.resume && <span className="row-resume">{row.resume}</span>}
              </div>
            </div>
          </div>

          <div className={`focus-state focus-state-${st.kind}`}>
            <span className="dot" />{st.text}
          </div>

          {inline.rec && inline.rec.questions.length > 0 && (
            <div className="focus-answers"
              onKeyDown={(e) => { if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); approve() } }}>
              <QuestionList questions={inline.rec.questions} answers={inline.answers}
                setAns={(id, v) => inline.setAnswers((a) => ({ ...a, [id]: v }))}
                setAll={inline.setAnswers} emptyHint="empty stays an open question" />
            </div>
          )}

          <div className="focus-actions">
            {isEditable(row.bucket) && (
              <button className="btn btn-primary" onClick={() => onOpen(row)}>
                Open & answer <kbd>↵</kbd>
              </button>
            )}
            {primary && (
              <button className="btn btn-go" disabled={busy}
                onClick={() => (primary.target === 'filled' ? approve() : hit(primary.target))}>
                {primary.label} <kbd>{primary.hot.toUpperCase()}</kbd>
              </button>
            )}
            {row.url && (
              <a className="btn" href={row.apply_url || row.url} target="_blank" rel="noreferrer">
                Open the form ↗ <kbd>O</kbd>
              </a>
            )}
            {row.url && row.apply_url && row.apply_url !== row.url && (
              <a className="btn btn-ghost" href={row.url} target="_blank" rel="noreferrer">JD ↗</a>
            )}
          </div>

          <div className="focus-secondary">
            {rest.map((m) => (
              <button key={m.target} className={`btn btn-sm btn-ghost ${m.target === 'blocked' ? 'btn-danger' : ''}`}
                onClick={() => hit(m.target)}>
                {m.label} {m.hot && <kbd>{m.hot.toUpperCase()}</kbd>}
              </button>
            ))}
            <div className="spacer" />
            {lastMove && (
              <button className="btn btn-sm undo-btn" onClick={undo}>
                ↩ undo “{lastMove.label}” <kbd>U</kbd>
              </button>
            )}
          </div>
        </article>

        <button className="focus-arrow" onClick={() => go(1)} disabled={idx >= n - 1}
          aria-label="next role">›</button>
      </div>

      <div className="focus-dots" aria-hidden="true">
        {rows.slice(Math.max(0, idx - 6), idx + 7).map((r) => (
          <span key={r.dedup_key}
            className={`fdot fdot-${r.bucket} ${r.dedup_key === row.dedup_key ? 'fdot-on' : ''}`} />
        ))}
      </div>

      {help && (
        <div className="keysheet">
          {KEYS.map(([k, what]) => (
            <div key={k} className="keyrow"><kbd>{k}</kbd><span>{what}</span></div>
          ))}
        </div>
      )}

      {idx + 1 < n && (
        <div className="focus-next">
          {rows.slice(idx + 1, idx + 4).map((r) => (
            <button key={r.dedup_key} className="upnext"
              onClick={() => setI(rows.indexOf(r))}>
              <Avatar name={r.company} size="sm" />
              <span className="upnext-co">{r.company}</span>
              <span className="upnext-role">{r.title}</span>
              <span className="upnext-date">{shortDate(r.date)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
