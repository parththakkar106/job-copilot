// The ⋯ reclassify menu, shared by the list rows and the board cards.
//
// It lives outside both views on purpose: on the board, drag-and-drop must never be the
// ONLY way to move a card — dragging is inaccessible to keyboard users, awkward on a
// trackpad and impossible on a touchscreen, so every card keeps this menu.

import React, { useState } from 'react'
import { useDismiss } from './ui.jsx'
import { MOVES } from './piles.js'

export default function MoveMenu({ row, onMove, up }) {
  const [open, setOpen] = useState(false)
  const ref = useDismiss(open, () => setOpen(false))
  const moves = MOVES[row.bucket] || []
  if (!moves.length) return null
  return (
    <div className="move" ref={ref}>
      <button className="btn btn-sm btn-icon" onClick={(e) => { e.stopPropagation(); setOpen((o) => !o) }}
        title="move to another pile">⋯</button>
      {open && (
        // `up` flips the menu above the button for cards near the bottom of a tall column,
        // where a downward menu would open past the column's scroll edge.
        <div className={`move-menu ${up ? 'move-menu-up' : ''}`} onClick={(e) => e.stopPropagation()}>
          {moves.map((m) => (
            <button key={m.target} className={m.target === 'blocked' ? 'danger' : ''}
              onClick={() => { setOpen(false); onMove(row, m) }}>
              {m.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
