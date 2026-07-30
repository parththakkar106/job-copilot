// Small shared UI primitives: company avatars, date formatting, keyboard shortcuts.

import React, { useEffect, useRef } from 'react'

// ── Company avatars ──────────────────────────────────────────────────────────────────
// No logo fetching (that would mean network calls out of a local-only console), so each
// company gets initials on a gradient derived from its name — stable across reloads, and
// distinct enough that you recognise a company by colour before you read it.

const HUES = [265, 210, 155, 32, 350, 190, 285, 120, 15, 240, 175, 305]

function hash(s) {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = (h * 0x01000193) >>> 0 }
  return h
}

export function initials(name) {
  const words = (name || '?').replace(/[^\w\s]/g, ' ').trim().split(/\s+/).filter(Boolean)
  if (!words.length) return '?'
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase()
  return (words[0][0] + words[1][0]).toUpperCase()
}

export function Avatar({ name, size = 'md' }) {
  const h = HUES[hash(name || '') % HUES.length]
  const style = {
    background: `linear-gradient(145deg, hsl(${h} 62% 46%), hsl(${(h + 34) % 360} 58% 34%))`,
    boxShadow: `0 2px 10px hsl(${h} 60% 30% / .45)`,
  }
  return <div className={`avatar avatar-${size}`} style={style} aria-hidden="true">{initials(name)}</div>
}

// ── Dates ────────────────────────────────────────────────────────────────────────────

const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

/** "2026-07-24" -> "jul 24". Returns '' for a missing/unparseable date. */
export function shortDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '')
  if (!m) return ''
  return `${MONTHS[+m[2] - 1]} ${+m[3]}`
}

/** "2026-07-24" -> "today" / "3d ago" / "5w ago". '' if undateable. */
export function relDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '')
  if (!m) return ''
  const then = Date.UTC(+m[1], +m[2] - 1, +m[3])
  const now = new Date()
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())
  const days = Math.round((today - then) / 86400000)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 21) return `${days}d ago`
  if (days < 60) return `${Math.round(days / 7)}w ago`
  return `${Math.round(days / 30)}mo ago`
}

// ── Keyboard shortcuts ───────────────────────────────────────────────────────────────

/**
 * Bind single-key shortcuts. `map` is { key: handler }, keys lowercased.
 *
 * Never fires while focus is in a text field or when `enabled` is false — the drawer
 * passes enabled=false so typing an essay answer can't skip the role out from under you.
 */
export function useHotkeys(map, enabled = true) {
  const ref = useRef(map)
  ref.current = map
  useEffect(() => {
    if (!enabled) return
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target
      const tag = (t?.tagName || '').toLowerCase()
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || t?.isContentEditable) return
      const fn = ref.current[e.key.toLowerCase()] || ref.current[e.key]
      if (fn) { e.preventDefault(); fn(e) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [enabled])
}

/** Close-on-outside-click / Escape for popovers. Returns a ref to put on the wrapper. */
export function useDismiss(open, onClose) {
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return
    const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) onClose() }
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, onClose])
  return ref
}
