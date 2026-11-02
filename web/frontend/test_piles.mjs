#!/usr/bin/env node
/**
 * Unit tests for the board's pure logic in src/piles.js.
 *
 * `canDrop` is what decides whether a drag is allowed to land, so a wrong answer is either
 * a drop the server will 400 on or a legal move the board silently refuses. `VIEW_KEY`
 * migration is here too: a stored view name that no longer exists must not strand the
 * console on a blank screen.
 *
 * No test runner in this frontend (zero runtime deps, and vitest would be the first dev
 * harness) — same plain-node style as extension/test_fill_units.mjs.
 *
 * Run: node web/frontend/test_piles.mjs
 */
import { COLUMNS, MOVES, canDrop, dropMove, isEditable, inverseMove, primaryMove } from "./src/piles.js";

let n = 0;
function ok(cond, what) {
  n++;
  if (!cond) { console.error(`FAIL: ${what}`); process.exit(1); }
}

// ── canDrop truth table ──────────────────────────────────────────────────────────────
ok(canDrop("in_queue", "filled"), "in_queue -> filled is the core approve drag");
ok(canDrop("in_queue", "submitted"), "in_queue -> submitted (applied by hand)");
ok(canDrop("in_queue", "blocked"), "in_queue -> blocked");
ok(canDrop("filled", "in_queue"), "filled -> in_queue sends it back to the queue");
ok(canDrop("blocked", "submitted"), "blocked -> submitted (actually applied)");
ok(canDrop("submitted", "blocked"), "submitted -> blocked (marked rejected)");
ok(canDrop("submitted", "in_queue"), "submitted -> in_queue un-sends it");
ok(canDrop("dream", "submitted"), "a dream role can be marked applied by hand");
ok(canDrop("manual", "submitted"), "a no-adapter role can be marked applied by hand");
ok(canDrop("discovered", "blocked"), "a discovered role can be marked not-interested");

ok(!canDrop("in_queue", "in_queue"), "a drop on its own column is a no-op, not a move");
ok(!canDrop("filled", "filled"), "same, from filled");
// discovered/dream have no pending record, so there's nothing to approve into Ready.
ok(!canDrop("discovered", "filled"), "discovered -> filled has no record to fill");
ok(!canDrop("discovered", "in_queue"), "discovered -> in_queue is not a transition");
ok(!canDrop("dream", "filled"), "dream -> filled has no record to fill");
// 'manual' means the extension has no adapter for the board, so Ready would be a lie:
// there is nothing to fill and nothing for a flush to send.
ok(!canDrop("manual", "filled"), "manual -> filled has no board we can fill");
// The two derived columns are never destinations, from anywhere.
for (const from of ["in_queue", "filled", "submitted", "blocked", "discovered", "dream",
                    "manual"]) {
  ok(!canDrop(from, "dream"), `${from} -> dream is never droppable (derived from dream.json)`);
  ok(!canDrop(from, "manual"), `${from} -> manual is never droppable (derived from manual.json)`);
  ok(!canDrop(from, "discovered"), `${from} -> discovered is never droppable (derived from queue.json)`);
}
ok(!canDrop(undefined, "filled"), "a drag with no source bucket can't land");
ok(!canDrop("in_queue", "nonsense"), "an unknown column id is never a target");
// `skipped` is a real transition but has no column — it must not leak in as a drop target.
ok(MOVES.in_queue.some((m) => m.target === "skipped"), "MOVES still carries the skip transition");
ok(!COLUMNS.some((c) => c.id === "skipped"), "…and there is deliberately no skipped column");
ok(!canDrop("in_queue", "skipped"), "so skipped is not reachable by a drop");

// ── dropMove hands the board the same descriptor the menus use ───────────────────────
ok(dropMove("in_queue", "filled").label === "Approve → Ready", "dropMove returns the MOVES entry");
ok(dropMove("in_queue", "in_queue") === null, "dropMove is null when the drop is illegal");
ok(dropMove("discovered", "filled") === null, "dropMove is null for a derived-source illegal drop");

// Every legal drop the board can produce must resolve to a real move descriptor: this is
// what guarantees the board can't invent a transition apply_transition doesn't handle.
for (const from of COLUMNS.map((c) => c.id)) {
  for (const to of COLUMNS.filter((c) => c.droppable).map((c) => c.id)) {
    if (canDrop(from, to)) ok(dropMove(from, to)?.target === to, `${from} -> ${to} resolves`);
  }
}

// ── Column shape ─────────────────────────────────────────────────────────────────────
ok(COLUMNS.length === 9, "nine columns");
ok(COLUMNS.filter((c) => c.droppable).length === 5, "five of them accept drops");
ok(["discovered", "in_queue", "filled", "manual"].every((b) => canDrop(b, "referral")),
   "every to-apply pile except Dream can park in Awaiting referral");
ok(COLUMNS.every((c) => c.droppable || c.hideWhenEmpty),
   "the non-droppable columns are exactly the ones hidden when empty");
ok(!isEditable("submitted") && isEditable("in_queue") && isEditable("filled"),
   "only queue-side cards open the drawer");

// ── The rail, and the one-click primary move ─────────────────────────────────────────
// Rails are laid out beside the column grid rather than inside it, one per edge. Two on the
// same edge would stack in the same 232px strip, so each side has exactly one.
const rails = COLUMNS.filter((c) => c.rail);
ok(rails.length === 3, "three rail columns");
ok(COLUMNS.filter((c) => c.rail === "left").map((c) => c.id).join() === "referral,manual",
   "Awaiting referral and By hand are the left rails");
ok(COLUMNS.find((c) => c.rail === "right")?.id === "expired", "Expired is the right rail");
ok(rails.filter((c) => c.droppable).map((c) => c.id).join() === "referral",
   "only Awaiting referral takes drops among the rails");

// A primary move must be a REAL move for that bucket, or the button would post a transition
// the server rejects.
ok(primaryMove("in_queue")?.target === "filled", "in_queue's one-click action is approve");
ok(MOVES.in_queue.some((m) => m === primaryMove("in_queue")),
   "the primary move is one of the bucket's own moves, not a second source of truth");
ok(canDrop("in_queue", primaryMove("in_queue").target),
   "clicking approve and dragging to Ready are the same transition");
for (const b of Object.keys(MOVES)) {
  const p = primaryMove(b);
  ok(p === null || MOVES[b].includes(p), `${b}'s primary move (if any) is one of its moves`);
}
ok(primaryMove("manual") === null && primaryMove("dream") === null,
   "the by-hand piles offer no approve — there is nothing to fill and nothing to send");
ok(primaryMove("submitted") === null, "an already-sent role has nothing to approve");

// ── VIEW_KEY migration ───────────────────────────────────────────────────────────────
// Mirrors Dashboard.jsx: an unknown stored value falls back to focus rather than rendering
// nothing. Kept in step by name — if the view list grows, this list grows.
const VIEWS = ["focus", "kanban", "list"];
const readView = (stored) => (VIEWS.includes(stored) ? stored : "focus");
ok(readView("kanban") === "kanban", "a stored kanban view survives a reload");
ok(readView("list") === "list", "a stored list view survives a reload");
ok(readView("board") === "focus", "an old/renamed view name falls back to focus");
ok(readView(null) === "focus", "a first-run empty localStorage falls back to focus");

// inverseMove is what undo leans on, and the board calls the same move() path.
ok(inverseMove("in_queue").target === "in_queue", "undo puts a queued role back in the queue");
ok(inverseMove("dream").target === "in_queue", "dream has no transition, so undo goes via in_queue");
ok(inverseMove("manual").target === "in_queue", "manual has no transition either");
ok(inverseMove("manual").label === "By hand", "and undo names the pile it came from");

console.log(`piles units: ${n}/${n} checks passed`);
