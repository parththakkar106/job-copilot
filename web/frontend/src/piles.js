// The pile model, shared by the focus, board and list views so they can't drift apart.
// Mirrors web/app/services.py (dashboard buckets) and docs/dashboard_spec.md §2.

/** Buckets still waiting on Parth to apply. Matches services.TO_APPLY. */
export const TO_APPLY = ['discovered', 'in_queue', 'filled', 'dream', 'manual']

export const BUCKETS = [
  { id: 'to_apply', label: 'To apply', of: TO_APPLY },
  { id: 'discovered', label: 'Discovered' },
  { id: 'in_queue', label: 'In queue' },
  { id: 'filled', label: 'Ready' },
  { id: 'dream', label: 'Dream' },
  { id: 'manual', label: 'By hand' },
  { id: 'submitted', label: 'Sent' },
  { id: 'blocked', label: 'Blocked' },
  { id: 'all', label: 'Everything' },
]

export const BUCKET_LABEL = {
  discovered: 'Discovered', in_queue: 'In queue', filled: 'Ready',
  dream: 'Dream', manual: 'By hand', submitted: 'Sent', blocked: 'Blocked',
}

/**
 * Which pile-moves make sense from each bucket.
 *
 * `hot` is the single-key shortcut in focus mode. No move carries a confirm prompt: every
 * one is reversible (blocking archives the record rather than deleting it) and the views
 * offer an undo, so a modal on each move was pure friction.
 *
 * `discovered`, `dream` and `manual` roles have no pending record on disk — only the
 * applied-log moves work on them, which is why they get a shorter list.
 */
export const MOVES = {
  discovered: [
    { target: 'submitted', label: 'Mark applied by hand', hot: 'm' },
    { target: 'blocked', label: 'Not interested', hot: 'b' },
  ],
  in_queue: [
    { target: 'filled', label: 'Approve → Ready', hot: 'a', primary: true },
    { target: 'skipped', label: 'Skip for now', hot: 's' },
    { target: 'submitted', label: 'Mark applied by hand', hot: 'm' },
    { target: 'blocked', label: 'Not interested', hot: 'b' },
  ],
  filled: [
    { target: 'in_queue', label: 'Back to queue' },
    { target: 'skipped', label: 'Skip for now', hot: 's' },
    { target: 'submitted', label: 'Mark applied by hand', hot: 'm' },
    { target: 'blocked', label: 'Not interested', hot: 'b' },
  ],
  dream: [
    { target: 'submitted', label: 'Mark applied by hand', hot: 'm' },
    { target: 'blocked', label: 'Not interested', hot: 'b' },
  ],
  // No adapter for the board, so 'Approve → Ready' would be a lie: there is nothing to
  // fill and nothing to send. Applying is something Parth does in the tab himself.
  manual: [
    { target: 'submitted', label: 'Mark applied by hand', hot: 'm' },
    { target: 'blocked', label: 'Not interested', hot: 'b' },
  ],
  submitted: [
    { target: 'in_queue', label: 'Un-send (back to queue)' },
    { target: 'blocked', label: 'Mark rejected', hot: 'b' },
  ],
  blocked: [
    { target: 'in_queue', label: 'Un-block (back to queue)' },
    { target: 'submitted', label: 'Actually applied', hot: 'm' },
  ],
}

/**
 * The move that puts a row back in the bucket it came FROM.
 *
 * `discovered`, `dream` and `manual` aren't transition targets — they're derived from the
 * scraped files. The way back is the in_queue transition, which drops the applied-log row;
 * the dashboard then re-surfaces the role from queue.json / dream.json / manual.json.
 */
const DERIVED = ['discovered', 'dream', 'manual']

export function inverseMove(fromBucket) {
  const back = DERIVED.includes(fromBucket) ? 'in_queue' : fromBucket
  return { target: back, label: BUCKET_LABEL[fromBucket] || 'the queue' }
}

/** A role is editable (has a pending record to open) only in these buckets. */
export const isEditable = (bucket) => bucket === 'in_queue' || bucket === 'filled'

/**
 * Board columns, in left-to-right order.
 *
 * `droppable: false` means the column is not a transition TARGET — `discovered`, `dream`
 * and `manual` are derived from the scraped files (out/queue.json, out/dream.json,
 * out/manual.json), so there's no state to move a role INTO. Their cards still drag OUT
 * (a by-hand role can be marked applied).
 *
 * `hideWhenEmpty` keeps the derived columns off the board on a day when they read 0, so
 * the four columns that matter get the width.
 *
 * `rail: true` pulls the column out of the grid entirely and parks it as a collapsible rail
 * down the left edge. `manual` is the one that gets this: those roles are not IN the
 * automated flow — nothing fills them, nothing sends them, Parth applies in the tab himself
 * — so they want to be a reachable list, not a full column of board width.
 */
export const COLUMNS = [
  { id: 'manual', label: 'By hand', droppable: false, hideWhenEmpty: true, rail: true },
  { id: 'discovered', label: 'Discovered', droppable: false, hideWhenEmpty: true },
  { id: 'in_queue', label: 'In queue', droppable: true },
  { id: 'filled', label: 'Ready', droppable: true },
  { id: 'dream', label: 'Dream', droppable: false, hideWhenEmpty: true },
  { id: 'submitted', label: 'Sent', droppable: true },
  { id: 'blocked', label: 'Blocked', droppable: true },
]

/**
 * The single move a card should offer as a one-click button, rather than burying it in ⋯.
 *
 * Only `in_queue` has one today ('Approve → Ready'), and that is the point: approving is
 * the move Parth makes dozens of times a day, and it was reachable on the board only by
 * dragging or by opening a hover-only menu — neither of which reads as "there is an
 * approve here". Anything without a `primary` move keeps just the ⋯ menu.
 */
export function primaryMove(bucket) {
  return (MOVES[bucket] || []).find((m) => m.primary) || null
}

/**
 * Can a card sitting in `fromBucket` legally be dropped on the `target` column?
 *
 * MOVES stays the source of truth — the column only names a destination. `MOVES.in_queue`
 * also lists `skipped`, which is no column; nothing can match it, so it drops out for free.
 */
export function canDrop(fromBucket, target) {
  if (!fromBucket || fromBucket === target) return false
  if (!COLUMNS.some((c) => c.id === target && c.droppable)) return false
  return (MOVES[fromBucket] || []).some((m) => m.target === target)
}

/** The move descriptor for a legal card→column drop, or null. Feeds straight into move(). */
export function dropMove(fromBucket, target) {
  if (!canDrop(fromBucket, target)) return null
  return (MOVES[fromBucket] || []).find((m) => m.target === target) || null
}
