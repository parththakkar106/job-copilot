# Kanban board — third console view (implementation plan)

Status: **BUILT 2026-07-25.** Plan written earlier the same day; this section records what
actually shipped and where it diverged. The rest of the document is the original plan, kept
because its reasoning (why native DnD, why not virtualise) still explains the code.
Companion to `dashboard_spec.md` (the pile model) — read that first.

## What shipped

`web/frontend/src/pages/Kanban.jsx` (new), `MoveMenu.jsx` (new — lifted out of
`Dashboard.jsx` so both the list rows and the board cards use one menu), `piles.js`
(+`COLUMNS`, `canDrop`, `dropMove`), `Dashboard.jsx` (three-way view + optimistic override +
segmented control), `Focus.jsx` (takes the switcher in place of its own ☰ list button),
`styles.css`, and `web/frontend/test_piles.mjs` (58 checks, plain node — no test runner
added). Backend untouched, as predicted.

Deviations from the plan below:

- **`dropMove(from, target)` was added next to `canDrop`.** The board needs the actual MOVES
  entry (for its `label`, which the undo bar shows), not just a yes/no — and resolving it
  through the same table is what guarantees the board cannot invent a transition
  `apply_transition` doesn't handle. A test asserts every legal drop resolves.
- **The board sizes itself by flexing, not `calc(100vh - X)`.** The stat strip and toolbar
  above it wrap at narrow widths, and a hardcoded offset pushed the last row of cards below
  the fold exactly when they did. `.content:has(.kanban)` becomes a flex column and the
  board takes `flex: 1`. It also lifts the 1180px content cap to 1560px — six columns need it.
- **Cards show the resume *track* ("TECH"), not the filename.** `Parth_Resume_t.pdf (tech
  track)` wrapped the meta line and made every card a different height. `.kb-meta` is now
  `white-space: nowrap` with the tag truncating and the date never doing so.
- **The ⋯ menu flips upward for the last cards in a column** (`up` prop → `.move-menu-up`),
  or it opened past the column's scroll edge.
- Tier + search filters apply to the board; the pile chips remain list-only (filtering a
  board to one pile just empties five columns).

Verified live against a **throwaway console on an isolated temp data layer** (fake records
in all six buckets) rather than by clicking Parth's real rows — see §10. Confirmed there:
optimistic landing, real persistence, the Ready refusal snapping a card back with a toast
naming the company, `discovered→Ready` and `*→Dream` both refused, `discovered→Sent`
allowed, a drag-out emptying the Discovered column and auto-hiding it, undo round-tripping
Blocked→Sent→Blocked, `1/2/3` switching + persisting, and the drawer opening for queue-side
cards only. Full Python suite still green (13 files, 274 checks).

The console currently has two views: **focus** (default) and **list**. This adds a third,
**kanban**, so the order becomes `focus → kanban → list`. Kanban is the "see the shape of the
whole pipeline" view; focus is for burning through the queue; list is for search and bulk
scanning. None replaces another.

Known trade-off, accepted going in: with ~29 roles in *To apply*, that column is very tall,
and trackpad drag-and-drop is slower than pressing `A`/`S` in focus mode. Mitigations are in
§6 and §7. Don't let kanban become the default.

---

## 1. What already exists (don't rebuild it)

Everything below shipped in `95d1bcb` and kanban should reuse it as-is:

| Piece | File | Notes |
|---|---|---|
| Pile model, labels, legal moves | `web/frontend/src/piles.js` | `BUCKETS`, `BUCKET_LABEL`, `MOVES`, `TO_APPLY`, `inverseMove`, `isEditable` |
| `move()` + undo + optimistic exit | `web/frontend/src/pages/Dashboard.jsx` | already passed as a prop into `Focus`; pass the same into `Kanban` |
| Avatars, dates, hotkeys, dismiss | `web/frontend/src/ui.jsx` | `Avatar`, `shortDate`, `relDate`, `useHotkeys`, `useDismiss` |
| Transitions (server) | `web/app/services.py` `apply_transition` | **no backend change is needed for kanban** |
| Job drawer | `web/frontend/src/pages/JobDrawer.jsx` | open on card click, same as list/focus |

`apply_transition` already handles every move kanban can produce, is reversible in both
directions, and archives rather than deletes. Kanban is a **pure frontend addition.**

---

## 2. Columns

Columns are buckets, but **the column id is not always the transition target**. `MOVES` in
`piles.js` is the source of truth for what's legal; the column just names a destination.

| Column | Bucket(s) shown | Drop target | Droppable? |
|---|---|---|---|
| Discovered | `discovered` | — | **No** — derived from `out/queue.json`, not a state you can move *into* |
| In queue | `in_queue` | `in_queue` | Yes |
| Ready | `filled` | `filled` | Yes (server refuses if questions are unanswered — surface the 400) |
| Dream | `dream` | — | **No** — derived from `out/dream.json` |
| Sent | `submitted` | `submitted` | Yes |
| Blocked | `blocked` | `blocked` | Yes |

Two derived columns are read-only *as destinations* but their cards are **draggable out**
(a discovered role can be marked applied-by-hand or blocked). Render them with a distinct
header treatment so it's obvious you can't drop into them.

Suggested default layout: show `In queue`, `Ready`, `Sent`, `Blocked` always; show
`Discovered` and `Dream` only when non-empty (both are 0 most days).

### Add to `piles.js`

```js
/** Columns in board order, with whether a card can be dropped INTO them. */
export const COLUMNS = [
  { id: 'discovered', label: 'Discovered', droppable: false, hideWhenEmpty: true },
  { id: 'in_queue',   label: 'In queue',   droppable: true  },
  { id: 'filled',     label: 'Ready',      droppable: true  },
  { id: 'dream',      label: 'Dream',      droppable: false, hideWhenEmpty: true },
  { id: 'submitted',  label: 'Sent',       droppable: true  },
  { id: 'blocked',    label: 'Blocked',    droppable: true  },
]

/** Can a card sitting in `fromBucket` legally be dropped on `target`? */
export function canDrop(fromBucket, target) {
  if (fromBucket === target) return false
  return (MOVES[fromBucket] || []).some((m) => m.target === target)
}
```

`canDrop` is the one piece of genuinely new logic — write a unit test for it (§8).
Note `MOVES.in_queue` contains `skipped`, which is not a column; `canDrop` naturally
excludes it because no column has that id.

---

## 3. Drag and drop: use native HTML5 DnD

**Do not add a library** (`dnd-kit`, `react-beautiful-dnd`). The board only needs
card→column drops, not sortable-within-column ordering, and the console has zero runtime
deps today beyond React. Native `draggable` covers it in ~40 lines.

```jsx
// card
<article draggable
  onDragStart={(e) => {
    e.dataTransfer.setData('text/plain', row.dedup_key)
    e.dataTransfer.effectAllowed = 'move'
    onDragStart(row)                    // lifts the row into Kanban state
  }}
  onDragEnd={() => onDragStart(null)} />

// column
<section
  onDragOver={(e) => { if (legal) { e.preventDefault(); e.dataTransfer.dropEffect = 'move' } }}
  onDrop={(e) => { e.preventDefault(); if (legal) onMove(dragging, { target: col.id, label: col.label }) }} />
```

Gotchas that will bite:

- **`onDragOver` must call `preventDefault()`** or the drop never fires. This is the single
  most common native-DnD mistake.
- Keep the dragged row in **React state**, not only in `dataTransfer` — `dataTransfer.getData`
  is unreadable during `dragover` in most browsers, and you need the source bucket to compute
  `canDrop` for the hover highlight.
- **`transform` on the card creates a stacking context.** This session already produced two
  layering bugs from exactly that (the move dropdown was clipped by `overflow:hidden`, then
  painted *under* later rows). Do not put `transform` on the drag ghost's ancestor, and give
  the hovered column an explicit `z-index`.
- Set a compact drag image (`e.dataTransfer.setDragImage`) or the browser drags a
  full-width card, which looks broken in a narrow column.

---

## 4. Optimistic movement

`move()` in `Dashboard.jsx` currently marks the row `leaving` (fades it out), awaits the API,
then refetches quietly. That reads correctly in a list where the row leaves the filter — but
on a board the card must **appear in the destination column immediately**, or the drop feels
broken for the ~200ms round trip.

Add an optimistic override in `Dashboard`, keyed by `dedup_key`:

```js
const [optimistic, setOptimistic] = useState({})   // dedup_key -> bucket
```

- On drop: `setOptimistic(o => ({...o, [key]: target}))` before the `await`.
- On success: clear that key after `load(true)` resolves (the refetch carries the real bucket).
- On failure: clear it, and let the existing error toast fire — the card snaps back, which is
  the correct signal.

Apply the override where `rows` is derived, so all three views agree:

```js
const rows = useMemo(() => data.rows.map(r =>
  optimistic[r.dedup_key] ? { ...r, bucket: optimistic[r.dedup_key] } : r), [data.rows, optimistic])
```

Note the `Ready` column can legitimately **reject** a drop (server 400: "answer its questions
first"). That's the main path where a card snaps back, so make sure the error toast names the
company — `move()` already does this.

---

## 5. View switching

`Dashboard.jsx` holds `const [view, setView] = useState(() => localStorage.getItem(VIEW_KEY) || 'focus')`.

- Accept `'kanban'` as a third value.
- **Migrate defensively**: a stored value that isn't one of the three must fall back to
  `'focus'`, or an old/garbage key strands the console on a blank view.
- Add a segmented control (focus / board / list) instead of the current one-off buttons —
  three views is where ad-hoc toggle buttons stop scaling. Put it top-right of the content
  area, visible in all three views.
- Hotkeys: `L` already switches focus→list. Add `B`… **no** — `B` is "not interested" in focus
  mode. Use `1`/`2`/`3` for focus/board/list globally, and keep `L` as-is for muscle memory.

---

## 6. Tall columns

29 cards in one column is the real usability risk.

- Each column body gets `overflow-y: auto` with its own scrollbar and a **sticky column
  header** showing the label + count, so the count stays visible while scrolling.
- Board height: `calc(100vh - <header+runbar+toolbar>)` so columns scroll independently
  rather than the page growing to the tallest column.
- Do **not** virtualise yet. 50 rows total is nowhere near needing it, and virtualisation
  fights native DnD auto-scroll. Revisit past ~300 rows.
- Auto-scroll while dragging near a column's top/bottom edge is a nice-to-have; skip it in v1.

---

## 7. Card contents

Keep cards dense — a board card shows less than a list row:

- Avatar (`<Avatar size="sm">`) + company (bold)
- Title, clamped to 2 lines (`-webkit-line-clamp: 2`)
- One meta line: `shortDate(row.date)` + the bucket tag (`row.tag`, e.g. "2 questions")
- `dream` tier badge when present
- The **`⋯` move menu stays on every card.** Drag-and-drop must not be the only way to
  reclassify: it's inaccessible to keyboard users, awkward on a trackpad, and impossible on
  mobile. The menu is already built and reuses `MOVES` — just drop `MoveMenu` onto the card.
- Click the card body → open `JobDrawer` (only when `isEditable(row.bucket)`, same rule as
  the list).

---

## 8. Tests

Backend is untouched, so no Python changes. Add frontend coverage for the new pure logic:

- `canDrop` truth table: `in_queue→filled` true, `in_queue→in_queue` false,
  `discovered→filled` false (no pending record), `blocked→submitted` true,
  `submitted→blocked` true, anything→`dream`/`discovered` false.
- The `VIEW_KEY` migration: unknown stored value → `'focus'`.

There's no JS test runner in `web/frontend` today. Either add `vitest` (dev-dep only, doesn't
ship in the bundle) or — cheaper and consistent with the repo's style — put `canDrop` through
a tiny node script like the `bridge/test_*.py` pattern. Prefer vitest if you expect more
frontend logic later.

Re-run the Python suite anyway as a regression check: all 13 files, 274 checks.

---

## 9. Order of work

1. `piles.js`: add `COLUMNS` + `canDrop` (+ test). No UI yet.
2. `Dashboard.jsx`: optimistic override in `rows`, three-way `view` state + migration,
   segmented control.
3. `Kanban.jsx`: static board, cards from `rows`, `MoveMenu` + drawer click. **No DnD yet** —
   this alone is already a usable view.
4. Add native DnD on top.
5. CSS: column grid, sticky headers, drop-target highlight, drag ghost.
6. `npm run build`, then verify against a **second** uvicorn instance on a spare port
   (`python -m uvicorn web.app.main:app --port 8010`) rather than the live console.

Step 3 is the natural stopping point if time runs short — a read-only board still delivers
most of the "see the pipeline" value.

---

## 10. Session gotchas worth remembering

- **Verify against a second instance on another port, and don't click real rows.** During the
  an earlier session a stray verification click actually skipped a live record (reverted
  via `pending.reopen`). Exercise moves through `web/test_api.py`'s isolated temp data layer,
  not the real board.
- **Killing the console process also kills the bridge** — they're one process tree under
  `start.py`. Restarting the console means the bridge restarts too.
- `overflow: hidden` on a card/row clips any popover positioned outside it; round the accent
  bar's own corners instead.
- The repo's CSS respects `prefers-reduced-motion` — any new drag animation must be inside
  that guard.
