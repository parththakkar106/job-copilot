import React, { useCallback, useMemo, useState } from 'react'
import { Avatar, relDate, shortDate } from '../ui.jsx'
import MoveMenu from '../MoveMenu.jsx'
import { COLUMNS, canDrop, dropMove, isEditable } from '../piles.js'

// The whole pipeline at a glance: one column per pile, drag a card to reclassify it.
//
// Focus mode is for burning through the queue and the list is for search — this view is for
// seeing the SHAPE of things (how much is waiting, how much went out). It's a pure frontend
// addition: every drop resolves to a move already in piles.MOVES, which services.py's
// apply_transition has always handled.
//
// Drag-and-drop is native HTML5, no library: the board only needs card→column drops, never
// ordering within a column, and that's ~40 lines against a dependency the console doesn't
// otherwise have.

/** "Parth_Resume_t.pdf (tech track)" -> "tech". A card has room for the track, not the file. */
function resumeTrack(resume) {
  const m = /\(([^)]+?)\s*track\)/i.exec(resume || '')
  return m ? m[1] : ''
}

function Card({ row, onOpen, onMove, onDragRow, dragging, index, menuUp }) {
  const editable = isEditable(row.bucket)
  const track = resumeTrack(row.resume)

  const onDragStart = (e) => {
    e.dataTransfer.setData('text/plain', row.dedup_key)
    e.dataTransfer.effectAllowed = 'move'
    // Without this the browser drags a full-width snapshot of the card, which looks broken
    // once it's floating over a narrow column. A compact chip reads as "carrying a role".
    const ghost = document.createElement('div')
    ghost.className = 'kb-ghost'
    ghost.textContent = `${row.company} — ${row.title}`
    document.body.appendChild(ghost)
    e.dataTransfer.setDragImage(ghost, 12, 16)
    setTimeout(() => ghost.remove(), 0)
    onDragRow(row)
  }

  return (
    <article draggable
      className={`kb-card kb-card-${row.bucket} ${dragging ? 'kb-card-dragging' : ''}`}
      style={{ animationDelay: `${Math.min(index, 12) * 14}ms` }}
      onDragStart={onDragStart}
      onDragEnd={() => onDragRow(null)}
      onClick={() => editable && onOpen(row)}
      role={editable ? 'button' : undefined}
      title={editable ? 'open & answer' : undefined}>
      <div className="kb-card-top">
        <Avatar name={row.company} size="sm" />
        <span className="kb-co">{row.company}</span>
        {row.tier === 'dream' && <span className="badge badge-dream">dream</span>}
        <div className="spacer" />
        <MoveMenu row={row} onMove={onMove} up={menuUp} />
      </div>
      <div className="kb-role">{row.title}</div>
      <div className="kb-meta">
        <span className={`tag tag-${row.bucket}`} title={row.tag}>{row.tag}</span>
        <div className="spacer" />
        {track && <span className="kb-track" title={row.resume}>{track}</span>}
        <span className="kb-date" title={`${row.date} — ${relDate(row.date)}`}>{shortDate(row.date)}</span>
      </div>
    </article>
  )
}

export default function Kanban({ rows, onOpen, onMove, switcher, lastMove, undo }) {
  const [drag, setDrag] = useState(null)   // the row under the cursor, source of truth for canDrop
  const [over, setOver] = useState(null)   // column currently highlighted

  const byCol = useMemo(() => {
    const g = {}
    for (const c of COLUMNS) g[c.id] = []
    for (const r of rows) if (g[r.bucket]) g[r.bucket].push(r)
    return g
  }, [rows])

  // Both derived columns read 0 on a normal day; showing them anyway would spend a sixth of
  // the board on empty space.
  const cols = COLUMNS.filter((c) => !c.hideWhenEmpty || byCol[c.id].length)

  const drop = useCallback((col) => {
    const row = drag
    setOver(null); setDrag(null)
    if (!row) return
    const m = dropMove(row.bucket, col.id)
    // The card lands optimistically; a server refusal (e.g. Ready with questions still
    // open) snaps it back and toasts, which is the right signal.
    if (m) onMove(row, m, { optimistic: col.id })
  }, [drag, onMove])

  return (
    <div className={`kanban ${drag ? 'kanban-dragging' : ''}`}>
      <div className="toolbar kb-toolbar">
        <div className="kb-hint">
          Drag a role to another pile, or use <span className="kb-key">⋯</span> on the card.
        </div>
        {switcher}
      </div>

      <div className="kb-board" style={{ '--kb-cols': cols.length }}>
        {cols.map((col) => {
          const legal = drag ? canDrop(drag.bucket, col.id) : false
          const hot = legal && over === col.id
          return (
            <section key={col.id}
              className={`kb-col kb-col-${col.id} ${hot ? 'kb-col-hot' : ''} ${drag && !legal ? 'kb-col-cold' : ''}`}
              onDragOver={(e) => {
                // Missing preventDefault here is THE native-DnD bug: without it no drop fires.
                if (!legal) return
                e.preventDefault()
                e.dataTransfer.dropEffect = 'move'
                if (over !== col.id) setOver(col.id)
              }}
              onDragLeave={(e) => {
                // dragleave also fires crossing into a child, so only clear when the pointer
                // has genuinely left the column.
                if (!e.currentTarget.contains(e.relatedTarget)) {
                  setOver((o) => (o === col.id ? null : o))
                }
              }}
              onDrop={(e) => { e.preventDefault(); drop(col) }}>
              <header className="kb-head">
                <span className={`kb-dot kb-dot-${col.id}`} />
                <span className="kb-label">{col.label}</span>
                <span className="kb-count">{byCol[col.id].length}</span>
                {!col.droppable && (
                  <span className="kb-derived"
                    title="derived from the scrape — drag cards out, but nothing drops in">auto</span>
                )}
              </header>
              <div className="kb-body">
                {byCol[col.id].map((r, i, all) => (
                  <Card key={r.dedup_key} row={r} index={i}
                    menuUp={all.length > 3 && i >= all.length - 2}
                    onOpen={onOpen} onMove={onMove}
                    onDragRow={setDrag} dragging={drag?.dedup_key === r.dedup_key} />
                ))}
                {!byCol[col.id].length && (
                  <div className="kb-empty">{hot ? 'drop here' : '—'}</div>
                )}
              </div>
            </section>
          )
        })}
      </div>

      {lastMove && (
        <div className="undobar">
          <span>Moved <strong>{lastMove.row.company}</strong> — {lastMove.label}</span>
          <button className="btn btn-sm" onClick={undo}>↩ Undo</button>
        </div>
      )}
    </div>
  )
}
