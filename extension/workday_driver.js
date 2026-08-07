// workday_driver.js — the multi-step driver for Workday application flows.
//
// WHY THIS IS A SEPARATE FILE. fill.js is built on one page = one fill = one report, which
// is true for Greenhouse, Lever and Ashby and false for Workday: six steps behind a
// client-side router, on a URL that never changes. Rather than thread a step concept
// through 2400 lines that do not need it, fill.js delegates the whole flow here when the
// adapter is workday, and the other three adapters keep working exactly as before.
//
// It uses the SAME primitives fill.js does (rpc/CDP, classify, dig, the resume attach), so
// there is one implementation of trusted input and one taxonomy resolver, not two.
//
// EVERY behaviour below was verified live on adobe.wd5.myworkdayjobs.com 2026-08-03 by
// walking a real application to step 3. The traps are documented in adapters/workday.js;
// the ones that shape THIS file:
//   * synthesized clicks work on listboxes but NOT on prompt trees or date spinners
//   * a synthesized date write displays but never commits, and poisons the field
//   * validation is server-side on Next, so the only proof a value took is that the step
//     actually advanced
//
// IT NEVER SUBMITS. The driver stops at step 6 (Review) and hands back. Submitting stays
// exactly what it is on every other adapter: a deliberate tap, or an approved queue-mode
// re-check. There is no code path here that clicks the final button.

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ── step awareness ───────────────────────────────────────────────────────────

export function currentStep(adapter) {
  const el = document.querySelector(adapter.multiStep.activeStep);
  if (!el) return null;
  const m = (el.innerText || "").match(adapter.multiStep.stepTextPattern);
  if (!m) return null;
  const name = (el.innerText || "").split("\n").pop().trim();

  // THE ENTRY PAGES CARRY THE SAME PROGRESS BAR. Observed live: /apply/autofillWithResume
  // reports "step 1 of 6" with the name "Autofill with Resume" — so a bare number match
  // says we are on My Information while the page is still asking for the PDF. The driver
  // then ran step 1's fill against it and reported "country: FAILED no listbox button",
  // which reads like a broken selector rather than the wrong page entirely.
  //
  // The NAME is the only thing that distinguishes them, since the number is 1 either way.
  const entryNames = (adapter.entry && adapter.entry.routePageNames) || [];
  if (entryNames.some(n => n.toLowerCase() === name.toLowerCase())) return null;

  return { n: Number(m[1]), of: Number(m[2]), name };
}

// Which route page we are parked on, read from the URL. Workday navigates to
// /apply/<routeKey> after the modal, so pressing Fill again lands here rather than on the
// advert — and the Apply button is long gone by then.
export function currentRoutePage(adapter) {
  const m = String(location.pathname).match(/\/apply\/([A-Za-z]+)\b/);
  if (!m) return null;
  const seg = m[1].toLowerCase();
  const routes = adapter.entry.routes;
  // The URL segment is Workday's automation-id, which is NOT always our key for it: the
  // manual route is keyed "manual" here and appears as /apply/applyManually. Match on both
  // so the naming choice in this file cannot desync from the live URL.
  return Object.keys(routes).find(k => {
    if (k.toLowerCase() === seg) return true;
    const aid = String(routes[k]).match(/data-automation-id="([^"]+)"/);
    return !!aid && aid[1].toLowerCase() === seg;
  }) || null;
}

// Wait for the step number to CHANGE from `from`. Workday re-renders in place, so polling
// the progress bar is the only signal; there is no navigation event to hook.
async function waitForStepChange(adapter, from, timeoutMs = 25000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const s = currentStep(adapter);
    if (s && s.n !== from) return s;
    await sleep(300);
  }
  return currentStep(adapter);
}

function stepErrors(adapter) {
  const sel = (adapter.multiStep.errorSelectors || []).join(",");
  if (!sel) return [];
  return [...document.querySelectorAll(sel)]
    .map(e => (e.innerText || "").replace(/\s+/g, " ").trim())
    .filter(Boolean);
}

// ── setters ──────────────────────────────────────────────────────────────────

function setTextNative(el, value) {
  const proto = el instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function fieldInput(wrapperSel, kind = "input,textarea") {
  const w = document.querySelector(wrapperSel);
  return w ? w.querySelector(kind) : null;
}

// Workday's simple dropdown. Synthesized clicks commit — verified on country (251
// options), state and degree.
//
// THE TRAP: a document-wide [role=option] query is wrong. `selectedItemList` is a second,
// permanently-present listbox holding `selectedItem` nodes for values chosen elsewhere on
// the page; on the country field it contributed a stray "India (+91)" owned by the phone
// widget. Scope to the real popup and drop selectedItem nodes.
export async function pickListbox(ctx, wrapperSel, wanted) {
  const { adapter } = ctx;
  const dd = adapter.dropdown;
  const btn = document.querySelector(`${wrapperSel} ${dd.control}`);
  if (!btn) return { ok: false, reason: "no listbox button" };

  // WHICH LIST BELONGS TO THIS BUTTON. A combobox names the listbox it controls, so when
  // the attribute is there the question has an exact answer and no heuristic is needed.
  const ownList = () => {
    const id = btn.getAttribute && (btn.getAttribute("aria-controls") || btn.getAttribute("aria-owns"));
    return id && document.getElementById ? document.getElementById(id) : null;
  };
  const readOpts = () => {
    const own = ownList();
    const boxes = own ? [own] : [...document.querySelectorAll(dd.optionScope)];
    const out = [];
    for (const b of boxes) out.push(...b.querySelectorAll(dd.option));
    return out.filter(o => !o.matches(dd.excludeOption) &&
                           !dd.placeholderText.test((o.innerText || "").trim()));
  };

  // WHOSE OPTIONS ARE THESE? Option lists render in a shared body-level portal, so a
  // document-wide read cannot tell this field's list from the PREVIOUS field's list that
  // has not been torn down yet. Reading one field's list while another's was still open
  // matched the wanted state against a COUNTRY of the same name and clicked it, changing
  // the country on a live application. Several US state names are also country names, so
  // it failed silently rather than loudly.
  //
  // With aria-controls above, that is settled exactly. WITHOUT it, fall back to the
  // invariant that one list is open at a time and wait for the previous one to go.
  // Excluding options by node identity was the first attempt at this and it does not
  // hold: Workday RECYCLES option nodes between lists, so a state rendered into a node
  // the country list had used looks stale and vanishes from the read — that is how a
  // 50-state list reported "no option matched (of 36)".
  const scoped = !!ownList();
  if (!scoped) for (let i = 0; i < 10 && readOpts().length; i++) await sleep(250);
  const leftOpen = scoped ? [] : readOpts();
  btn.click();

  const want = String(wanted).trim().toLowerCase();
  const txt = (o) => (o.innerText || "").trim();
  const match = (os) => os.find(o => txt(o).toLowerCase() === want)
                     || os.find(o => txt(o).toLowerCase().startsWith(want))
                     || os.find(o => txt(o).toLowerCase().includes(want));

  // Then wait for the list to SETTLE, not merely to be non-empty. A dependent list is
  // refetched when its parent changes — setting Country reloads State, and this driver
  // writes them back to back — and it populates progressively. Stopping at the first
  // non-empty read caught a US state list mid-flight at 36 entries and reported the
  // state missing; the entry was real, it just had not arrived. Two reads of the same
  // size mean it has stopped growing. Finding the answer ends the wait early.
  let opts = [], hit = null, prev = -1;
  for (let i = 0; i < 14; i++) {
    await sleep(250);
    opts = readOpts();
    hit = match(opts);
    if (hit) break;
    if (opts.length && opts.length === prev) break;      // settled without the answer
    prev = opts.length;
  }

  // If the previous list never closed, this one cannot be told apart from it. Skipping
  // costs an unfilled field; guessing cost a wrong country on a real application.
  if (leftOpen.length && opts.some(o => leftOpen.includes(o))) {
    btn.click();
    return { ok: false, wanted, reason: "another field's list was still open" };
  }

  if (!hit) {
    btn.click();                                   // close, leave the field untouched
    // An empty list and a list without the answer are different failures — one is timing,
    // the other is vocabulary — and the old message could not tell them apart.
    return { ok: false, wanted, sample: opts.slice(0, 8).map(txt),
             reason: opts.length ? `no option matched (of ${opts.length})`
                                 : "list never populated" };
  }
  hit.click();
  await sleep(600);

  // VERIFY. Returning btn.innerText as the value assumed the click worked, and once
  // reported `state | set Select One` — the placeholder, i.e. nothing selected at all.
  // A report that says "set" when the field is empty is worse than one that says failed.
  const now = (btn.innerText || "").trim();
  if (dd.placeholderText.test(now) || !now) {
    return { ok: false, wanted, reason: "clicked the option but the field stayed empty" };
  }
  return { ok: true, value: now, scope: scoped ? "own" : "document" };
}

// Workday's HIERARCHICAL PROMPT — the widget that forced needsCDP back on.
//
// Verified: `.click()` does nothing, and neither does a full synthetic
// pointerdown/mousedown/pointerup/mouseup/click with correct coordinates. Only OS-level
// clicks move it. Two levels: categories, then leaves. Three further traps, all live-hit:
//   * options render in a BODY-LEVEL portal, not inside the field
//   * the click must land on the ROW'S RADIO, not its text — text only highlights
//   * the panel collapses to root on blur, so nothing may steal focus between the two
//     clicks (read coordinates from the DOM, never via a round-trip that touches the page)
// WHICH element actually scrolls. The portal is the node the rows live in, but it is not
// necessarily the one with the overflow — scrolling a non-scrolling element moves nothing,
// the loop sees scrollTop unchanged, concludes it has hit the bottom, and gives up on the
// first pass. Measured on the live discipline list: only 9 of several hundred rows are
// rendered at a time, so failing to scroll means never seeing all but the first nine.
function scrollerFor(portalSel) {
  const portal = document.querySelector(portalSel);
  if (!portal) return null;
  // A collapsed wrapper reports scrollHeight >> clientHeight === 0 and looks scrollable by
  // arithmetic alone; the live chain has one sitting directly above the real grid. Scrolling
  // it moves nothing. Require a viewport with actual height.
  const canScroll = (e) => e && e.clientHeight >= 40 && e.scrollHeight > e.clientHeight + 5;
  if (canScroll(portal)) return portal;
  // Search DESCENDANTS, not just direct children. On the multiSelectContainer shape the
  // element with the overflow is a ReactVirtualized grid four levels down; checking only
  // one level finds a 39px-tall wrapper that reports scrollHeight === clientHeight, and
  // every scroll against it is a no-op the loop reads as "bottom reached".
  for (const el of portal.querySelectorAll("*")) if (canScroll(el)) return el;
  if (canScroll(portal.parentElement)) return portal.parentElement;
  return portal;                       // nothing obviously scrollable: try it anyway
}

// What to say when nothing matched. The last few rows on screen are near-useless — they
// are wherever scrolling stopped, which is why an earlier failure reported "Risk
// Management, Rural Sociology, Russian" for a search for Computer Science. Rows sharing a
// word with what was wanted are what a human needs to see to fix the profile.
export function nearest(wants, seen, limit = 6) {
  const words = new Set(wants.join(" ").toLowerCase().split(/[^a-z0-9]+/).filter(w => w.length > 3));
  const score = (t) => {
    const ws = t.toLowerCase().split(/[^a-z0-9]+/);
    return ws.filter(w => words.has(w)).length;
  };
  return seen.map(t => [score(t), t]).filter(([s]) => s > 0)
             .sort((a, b) => b[0] - a[0]).slice(0, limit).map(([, t]) => t);
}

// WHERE to click a row. The old answer was "22px in from the left edge", measured once on
// one tenant's referral list. That is a constant standing in for a fact the DOM already
// knows: leaves commit only when their radio is hit, and the radio is an element with a
// position. Ask it. The offset survives only as a fallback for a control the browser gives
// no box for (Workday hides the real input behind a styled span on some shapes), and a
// row whose own box is empty is reported rather than clicked at (0,0).
export function clickPointFor(row, pt, isLeaf) {
  const box = (e) => (e ? e.getBoundingClientRect() : null);
  const real = (r) => r && r.width > 0 && r.height > 0;
  const rr = box(row);
  if (!real(rr)) return null;

  if (isLeaf) {
    const ctrl = row.querySelector(pt.rowControl || 'input[type="radio"],input[type="checkbox"],[role="radio"],[role="checkbox"]');
    const cr = box(ctrl);
    if (real(cr)) return { x: Math.round(cr.left + cr.width / 2),
                           y: Math.round(cr.top + cr.height / 2), via: "control" };
    return { x: Math.round(rr.left + (pt.radioOffsetFromLeft ?? 22)),
             y: Math.round(rr.top + rr.height / 2), via: "offset" };
  }
  return { x: Math.round(rr.left + rr.width / 2),
           y: Math.round(rr.top + rr.height / 2), via: "row-centre" };
}

// Is the row actually the thing at that point? Returns null when the click will land, or a
// short description of whatever is on top when it will not.
export function whatIsAt(x, y, row) {
  if (typeof document.elementFromPoint !== "function") return null;   // cannot check: allow
  const top = document.elementFromPoint(x, y);
  if (!top) return "nothing (point is off-screen)";
  if (top === row || row.contains(top) || top.contains(row)) return null;
  const id = top.getAttribute && top.getAttribute("data-automation-id");
  return `<${top.tagName.toLowerCase()}${id ? ` ${id}` : ""}>`;
}

// Rows currently rendered, with the panel's EMPTY STATE removed. Workday shows a single
// row reading "No Items." when it has nothing; counted as an option it makes an empty list
// look populated, and a prefix match on a short term could click it.
export function allRows(pt) {
  const all = [...document.querySelectorAll(`${pt.portal} ${pt.row}`)];
  return all.filter(r => {
    const t = (r.innerText || "").trim();
    if (pt.emptyRow && pt.emptyRow.test(t)) return false;
    if (pt.headerRow && pt.headerRow.test(t)) return false;   // "Search Results (20)"
    return true;
  });
}
export function showsEmptyState(pt) {
  if (!pt.emptyRow) return false;
  return [...document.querySelectorAll(`${pt.portal} ${pt.row}`)]
    .some(r => pt.emptyRow.test((r.innerText || "").trim()));
}

// Whose panel is open: true (this field's), false (nobody's), or null (cannot tell).
// aria-expanded first because it is the standard; the live Skills input has none, but the
// wrapper's own text gains "Expanded" — an equally definite signal, just a Workday one.
export function panelOwner(wrap, opener, pt) {
  const aria = opener && opener.getAttribute && opener.getAttribute("aria-expanded");
  if (aria === "true") return true;
  if (aria === "false") return false;
  if (pt.expandedText && wrap) return pt.expandedText.test(wrap.innerText || "");
  return null;
}
export function isPanelOpen(wrap, opener, pt) {
  return panelOwner(wrap, opener, pt) === true;
}

export async function pickPromptTree(ctx, wrapperSel, path) {
  const { adapter, rpc, bringIntoView } = ctx;
  const pt = adapter.promptTree;
  const wrap = document.querySelector(wrapperSel);
  if (!wrap) return { ok: false, reason: "field not found" };
  if (!Array.isArray(path) || !path.length) return { ok: false, reason: "no path configured" };

  bringIntoView(wrap);
  const opener = wrap.querySelector("input") || wrap.querySelector(pt.field) || wrap;
  const r0 = opener.getBoundingClientRect();
  if (!r0.width && !r0.height) return { ok: false, reason: "opener has no rect" };

  const rows = () => allRows(pt);
  const rowText = (el) => (el.innerText || "").trim();

  // OPEN IT ONLY IF IT IS SHUT. The opener is a TOGGLE, so a multi-select being filled one
  // value at a time — Skills — leaves the panel open and a second unconditional click
  // closes it. That is the exact shape that made Field of Study report "no row matched"
  // against a list that was no longer there.
  if (!isPanelOpen(wrap, opener, pt)) {
    // Our panel is shut, so any rows on screen are SOMEONE ELSE'S — and rows are matched
    // document-wide, so reading them is how a neighbouring field's list gets acted on.
    // That mistake changed a country on a live application. Wait for them to go, and
    // refuse if they will not: an unfilled field costs less than a wrong one.
    for (let i = 0; i < 10 && rows().length; i++) await sleep(250);
    if (rows().length) {
      return { ok: false, reason: "another field's list is still open" };
    }
    await rpc({ type: "CDP_CLICK",
                x: Math.round(r0.left + r0.width / 2), y: Math.round(r0.top + r0.height / 2) });
    await sleep(900);
  }
  let lastHit = null, lastTarget = null;

  for (let depth = 0; depth < path.length; depth++) {
    // A level may offer ALTERNATES: the profile's own wording plus fallbacks, tried in
    // order. This is not fuzzy matching — every candidate is one the human wrote down.
    // Adobe's 320-entry Field of Study list has no "Computer Science" at all, so a
    // vocabulary gap is a normal outcome here, not a bug to paper over with a guess.
    const wants = (Array.isArray(path[depth]) ? path[depth] : [path[depth]])
      .map(v => String(v).trim()).filter(Boolean);
    const isLeaf = depth === path.length - 1;

    // The list is virtualized: only rendered rows exist in the DOM, so a target further
    // down needs scrolling before it can be found at all. Everything scrolled past is
    // remembered, both to match alternates that appeared earlier and to name near-misses
    // if nothing matches.
    const seen = [];
    let hit = null;
    for (let scroll = 0; scroll < (pt.maxScrolls || 120); scroll++) {
      for (const r of rows()) {
        const t = rowText(r);
        if (!seen.includes(t)) seen.push(t);
      }
      for (const want of wants) {
        const w = want.toLowerCase();
        hit = rows().find(x => rowText(x).toLowerCase() === w)
           || rows().find(x => rowText(x).toLowerCase().startsWith(w));
        if (hit) break;
      }
      if (hit) break;
      const list = scrollerFor(pt.portal);
      if (!list) break;
      const before = list.scrollTop;
      // Overlap deliberately. A full-viewport jump advances by exactly the number of rows
      // rendered, so a row straddling the boundary can fall between two frames and never
      // be queryable. Measured on Adobe's list: 220px viewport, 32px rows — 6.875 rows
      // visible, which does not divide evenly.
      list.scrollTop += Math.max(40, Math.round((list.clientHeight || 200) * 0.75));
      await sleep(250);
      if (list.scrollTop === before) break;         // bottom reached
    }
    if (!hit) {
      return { ok: false, reason: `no row matched at depth ${depth}`,
               wanted: wants, sawCount: seen.length, near: nearest(wants, seen) };
    }

    // CENTRE THE ROW IN ITS SCROLLER BEFORE CLICKING. A row is "found" as soon as it is
    // rendered, but the renderer keeps a partially-visible row at the viewport edge in the
    // DOM — and a click on the clipped half lands on whatever is drawn over it. Centring
    // is also what makes the coordinates stable enough to hit-test below.
    const list = scrollerFor(pt.portal);
    if (list) {
      const lr = list.getBoundingClientRect(), hr = hit.getBoundingClientRect();
      const delta = (hr.top + hr.height / 2) - (lr.top + lr.height / 2);
      if (Math.abs(delta) > 4) { list.scrollTop += delta; await sleep(300); }
      // RE-FIND IT. Virtualised rows are recycled, so scrolling can hand the old node to a
      // different option or unmount it entirely — the failure diagnostic caught exactly
      // that, reporting a 0x0 rect for a row that had been reused underneath us. Holding a
      // stale reference is how you click the wrong discipline.
      const w0 = wants.map(v => v.toLowerCase());
      hit = rows().find(x => w0.includes(rowText(x).toLowerCase()))
         || rows().find(x => w0.some(w => rowText(x).toLowerCase().startsWith(w)))
         || hit;
    }
    lastHit = hit;

    const target = clickPointFor(hit, pt, isLeaf);
    if (!target) {
      return { ok: false, reason: "row has no clickable box",
               widget: describePromptRow(wrap, pt, hit) };
    }
    // ASK THE PAGE WHAT IS AT THAT POINT instead of assuming. A trusted click goes to
    // whatever is topmost at the coordinate, so an overlay, a clipped edge or a stale rect
    // silently sends it somewhere else — indistinguishable, after the fact, from a click
    // that landed and did not commit. This is the check that turns that into a real error.
    const blocker = whatIsAt(target.x, target.y, hit);
    if (blocker) {
      return { ok: false, reason: `click point is covered by ${blocker}`,
               widget: { ...describePromptRow(wrap, pt, hit), target } };
    }
    await rpc({ type: "CDP_CLICK", x: target.x, y: target.y });
    await sleep(1100);
    lastTarget = target;
  }

  const c = committedValue(wrap, pt);
  return c ? { ok: true, value: c }
           : { ok: false, reason: "clicked through but nothing committed",
               widget: { ...describePromptRow(wrap, pt, lastHit), target: lastTarget } };
}

// Did the selection stick? Two signals, because one shape does not cover both users of
// this widget: a single-select tree renders a `selectedItem` pill, while the multi-select
// Field of Study only updates its own summary line to "N items selected". Reading the
// pill alone reports failure on a multi-select that worked perfectly.
export function committedValue(wrap, pt) {
  const pill = wrap.querySelector(pt.selected);
  if (pill && (pill.innerText || "").trim()) return (pill.innerText || "").trim();
  const m = (wrap.innerText || "").match(/(\d+)\s+items?\s+selected/i);
  if (m && Number(m[1]) > 0) return m[0];
  return null;
}

// Failure diagnostic, same bargain as describeDateWidget: a click that lands but does not
// commit is indistinguishable from one that missed, and every guess at the cause costs a
// live run. Report the geometry and the row's own children so the next run says which.
export function describePromptRow(wrap, pt, row) {
  const box = (e) => { const r = e.getBoundingClientRect();
    return { l: Math.round(r.left), t: Math.round(r.top),
             w: Math.round(r.width), h: Math.round(r.height) }; };
  const out = { field: (wrap.innerText || "").trim().slice(0, 60) };
  if (!row) return { ...out, row: "gone after click" };
  out.row = box(row);
  // A 0x0 row means the list recycled the node between the click and this read, not that
  // the row was invisible when clicked. Saying so saves the next reader the deduction.
  if (!out.row.w && !out.row.h) out.recycled = true;
  out.text = (row.innerText || "").trim().slice(0, 40);
  out.children = [...row.querySelectorAll("*")].slice(0, 12).map(e => ({
    tag: e.tagName.toLowerCase(),
    aid: e.getAttribute("data-automation-id"),
    role: e.getAttribute("role"),
    checked: e.getAttribute("aria-checked") ?? e.getAttribute("aria-selected") ?? e.checked,
    ...box(e),
  })).filter(e => e.w > 0 || e.aid || e.role);
  return out;
}

// Split month/year spinner.
//
// THE TRAP, and it cost the most to find: a synthesized native-setter write DISPLAYS the
// value — the field read back "current value is 1/2024" and looked right on screen — but
// never reaches Workday's validation store, so Next bounces with "The field From is
// required" while showing 01/2024. Worse, that write POISONS the section: typing over it
// with real keystrokes did not clear the error either. It has to be erased first, then
// typed with trusted keys. Never setNativeValue a Workday date.
// What is actually inside a date wrapper. Every date failure so far has been diagnosed by
// inferring the widget's shape from the error text on screen, which took a round trip each
// time. When a date will not commit, say what the control IS: its inputs, their
// automation-ids, and what they currently hold.
function describeDateWidget(wrap) {
  if (!wrap) return "no wrapper";
  const parts = [...wrap.querySelectorAll("input")].slice(0, 6).map(el => {
    const aid = el.getAttribute("data-automation-id") || el.getAttribute("aria-label") || el.name || "?";
    return `${aid}="${String(el.value || "").slice(0, 12)}"`;
  });
  const text = String(wrap.innerText || "").replace(/\s+/g, " ").trim().slice(0, 60);
  return parts.length ? `inputs: ${parts.join(", ")}` : `no inputs; text "${text}"`;
}

export async function setDate(ctx, wrapperSel, iso) {
  const { adapter, rpc, bringIntoView } = ctx;
  const d = adapter.date;
  const wrap = document.querySelector(wrapperSel);
  if (!wrap) return { ok: false, reason: "date field not found" };
  const m = String(iso || "").match(/^(\d{4})-(\d{1,2})/);
  if (!m) return { ok: false, reason: `unparseable date ${JSON.stringify(iso)} (want YYYY-MM)` };

  const mm = String(m[2]).padStart(2, "0"), yyyy = m[1];

  const month = wrap.querySelector(d.month);
  const year = wrap.querySelector(d.year);
  bringIntoView(wrap);

  // SINGLE-INPUT SHAPE. Not every Workday date control is a two-section spinner; some are
  // one box that wants "MM/YYYY" typed, slash and all. Six bare digits are invalid there,
  // which is consistent with the field rejecting everything it was given.
  if (!month || !year) {
    const one = wrap.querySelector('input');
    if (!one) return { ok: false, reason: `no date input at all — ${describeDateWidget(wrap)}` };
    const r = one.getBoundingClientRect();
    if (!r.width && !r.height) return { ok: false, reason: "the date input has no rect" };
    await rpc({ type: "CDP_CLICK",
                x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) });
    await sleep(200);
    await rpc({ type: "CDP_ERASE", times: d.clearKeystrokes || 8 });
    await sleep(150);
    await rpc({ type: "CDP_TYPE", text: `${mm}/${yyyy}` });
    await sleep(400);
    const got = String(one.value || "");
    if (got.includes(mm) && got.includes(yyyy)) return { ok: true, value: `${mm}/${yyyy}` };
    return { ok: false,
             reason: `single-box date did not commit — reads "${got}", wanted ${mm}/${yyyy}` };
  }

  // DRIVE EACH SECTION, DO NOT TRUST AUTO-ADVANCE. Typing all six digits into the month
  // section and letting the widget carry the rest into the year produced "MM/2005" plus
  // "Invalid Date: 05/" on a real form — the year took the tail and the month kept
  // nothing. Clicking each section and typing only its own digits removes the guess.
  // FOCUS THE MONTH BY CLICK, THEN MOVE BY KEYPRESS. Clicking the year section to move
  // there does NOT work: focus stayed on the month, so the year's digits went into a month
  // spinner and clamped to its maximum — the form read "Invalid Date: 12/" whatever date
  // was asked for. A caret move is what a person does and needs no coordinates.
  const focusMonth = async () => {
    const r = month.getBoundingClientRect();
    if (!r.width && !r.height) return false;
    await rpc({ type: "CDP_CLICK",
                x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) });
    await sleep(200);
    return true;
  };
  const reads = () => [String(month.value || "").trim().padStart(2, "0"),
                       String(year.value || "").trim()];

  // THREE STRATEGIES, tried in order, each verified. This control has failed in every
  // direction so far — a bare MMYYYY burst lost the month, clicking between sections
  // clamped the month to 12 — and there is no way to tell from here which shape a given
  // tenant renders. Verification is what makes trying several safe: a strategy that half
  // works is otherwise indistinguishable from one that works.
  // MEASURED, not guessed: the month commits and the year receives NOTHING. The slash does
  // not advance the section, ArrowRight does not move the caret, and a six-digit burst does
  // not spill over. Every one of those was an attempt to move focus off the month, and all
  // three failed the same way — so the answer is to put focus on the year directly instead
  // of trying to travel there.
  //
  // Input.insertText goes to whatever the renderer has focused, and el.focus() genuinely
  // moves renderer focus, so focusing the section needs no coordinates at all. That also
  // sidesteps the earlier clamp: a click aimed at the year landed on the month, and typing
  // a year into a month spinner pinned it to 12.
  const typeInto = async (el, text, erase) => {
    el.focus();
    await sleep(120);
    if (document.activeElement !== el) return false;
    await rpc({ type: "CDP_ERASE", times: erase });
    await sleep(120);
    await rpc({ type: "CDP_TYPE", text });
    await sleep(250);
    return true;
  };

  const strategies = [
    // Focus each section directly. No coordinates, no caret travel.
    async () => {
      if (!await typeInto(month, mm, 4)) return "the month section would not take focus";
      if (!await typeInto(year, yyyy, 6)) return "the year section would not take focus";
      return null;
    },
    // Tab between sections, in case the widget manages focus itself and rejects a
    // programmatic focus() on its inner inputs.
    async () => {
      if (!await focusMonth()) return "the month section has no rect";
      await rpc({ type: "CDP_ERASE", times: d.clearKeystrokes || 8 });
      await rpc({ type: "CDP_TYPE", text: mm });
      await sleep(200);
      await rpc({ type: "CDP_KEY", key: "Tab" });
      await sleep(150);
      await rpc({ type: "CDP_TYPE", text: yyyy });
      return null;
    },
    // Last resort: the separator, which is what the field displays.
    async () => {
      if (!await focusMonth()) return "the month section has no rect";
      await rpc({ type: "CDP_ERASE", times: d.clearKeystrokes || 8 });
      await rpc({ type: "CDP_TYPE", text: `${mm}/${yyyy}` });
      return null;
    },
  ];

  let lastWhy = null;
  for (const run of strategies) {
    lastWhy = await run();
    await sleep(350);
    const [gotM, gotY] = reads();
    if (gotM === mm && gotY === yyyy) return { ok: true, value: `${mm}/${yyyy}` };
  }

  // AND REPORT THE TRUTH. This used to return ok:true unconditionally, so the report said
  // "set" for dates that were showing validation errors on screen — the worst kind of
  // wrong, because the run moves on and blames the next step.
  const [m2, y2] = reads();
  return { ok: false,
           reason: `did not commit after ${strategies.length} strategies — reads ` +
                   `"${m2 || "MM"}/${y2 || "YYYY"}", wanted ${mm}/${yyyy}` +
                   (lastWhy ? ` (${lastWhy})` : "") + `; ${describeDateWidget(wrap)}` };
}

// ── steps ────────────────────────────────────────────────────────────────────

// Step 1 renders ONLY the country dropdown until a country commits; everything else is
// absent from the DOM. And the default is GEO-DERIVED, not empty — it arrived pre-set to
// "India" from the connection's IP. Always set it explicitly: treating a non-empty value
// as already-correct files US applications with a foreign country and poisons the
// work-authorization answers downstream.
async function fillMyInformation(ctx, report) {
  const { adapter, profile, dig } = ctx;
  const pre = adapter.prerequisite;
  const country = dig(profile, pre.profilePath) || pre.fallbackValue;

  // DELIBERATELY NOT RECONCILED. Every other field on this step is left alone when it
  // already looks right; country is set explicitly every time because its default is
  // derived from the connection's IP, not from nothing — it arrived pre-set to a foreign
  // country while the label still read "Select One". A non-empty value here is not
  // evidence of a correct one, and getting it wrong poisons the work-authorization
  // answers downstream.
  // WAIT FOR THE PAGE FIRST. The progress bar updates before the fields exist, so arriving
  // at the step is not the same as the step being ready — reading it too early reports
  // every control as missing, which is indistinguishable from a bad selector.
  let expanded = await waitFor(pre.thenWaitFor, 20000);

  // COUNTRY IS A PREREQUISITE ONLY WHILE THE FORM IS COLLAPSED. That is the manual flow:
  // nothing else renders until a country is picked. The autofill flow arrives with the
  // form already built from the parsed PDF, and there country is just another field —
  // sometimes not even present, having been captured during the parse. Treating it as a
  // hard gate there aborted the entire step over one absent control, which is exactly what
  // happened live: a lone "country FAILED no listbox button" and not one other field
  // touched.
  const countryWrapper = document.querySelector(`[data-automation-id="${pre.field}"]`);
  if (countryWrapper) {
    const r = await pickListbox(ctx, `[data-automation-id="${pre.field}"]`, country);
    report.push(["country", r.ok ? `set ${r.value}` : `FAILED ${r.reason}`]);
    if (!r.ok && !expanded) {
      report.push(["step1", "FAILED could not set country and the form is still collapsed"]);
      return;
    }
  } else if (!expanded) {
    report.push(["country", "FAILED control not found and the form never expanded"]);
    return;
  } else {
    report.push(["country", "not on this page — the resume parse already captured it"]);
  }

  // Manual flow: the fields appear only after country commits.
  if (!expanded) {
    expanded = await waitFor(pre.thenWaitFor, 6000);
    if (!expanded) {
      report.push(["step1", "FAILED form did not expand after country"]);
      return;
    }
  }

  for (const f of adapter.coreFields) {
    if (f.skip) continue;
    let value = f.value != null ? f.value : (f.profilePath ? dig(profile, f.profilePath) : null);
    if (value == null || value === "" || value === "FILL_ME") {
      if (!f.optional) report.push([f.key, "SKIPPED no value in profile"]);
      continue;
    }
    if (f.transform === "stripCountryCode") value = String(value).replace(/^\+\d+\s*/, "");

    // Reconcile before writing. The form arrived pre-filled by the clone or the resume
    // parser, so most of these are already right, and rewriting one is not free: a
    // dropdown costs a menu round-trip, a prompt tree two trusted clicks, and a date field
    // has to be cleared and retyped because a plain write poisons it.
    const current = readFieldValue(ctx, f);
    const kind = f.compare || f.control;
    const d = reconcileDecision(kind, current, value, adapter);
    if (d.action !== "write") {
      report.push([f.key, d.action === "keep" ? `kept "${String(current).trim()}"` : `skipped: ${d.why}`]);
      continue;
    }

    if (f.control === "listbox") {
      const wrapper = f.selectors[0].replace(/\s+button.*$/, "");
      const res = await pickListbox(ctx, wrapper, value);
      report.push([f.key, res.ok ? `set ${res.value}` : `FAILED ${res.reason}`]);
    } else if (f.control === "promptTree") {
      const path = resolveSourcePath(ctx, f, value);
      const res = await pickPromptTree(ctx, f.selectors[0], path);
      report.push([f.key, res.ok ? `set ${res.value}` : `FAILED ${res.reason}`]);
    } else {
      const el = document.querySelector(f.selectors.join(",")) ;
      if (!el) { report.push([f.key, "FAILED control not found"]); continue; }
      setTextNative(el, String(value));
      report.push([f.key, "set"]);
    }
  }
}

// "How Did You Hear About Us?" is tenant vocabulary, not a profile string — "Company
// website" simply does not exist on Adobe, whose nearest leaf is Adobe Source > Adobe.com.
// So the tenant map wins; the ladder is only a guess for unmapped tenants and will often
// need a human. Better to fail loudly here than to file a wrong answer silently.
function resolveSourcePath(ctx, field, value) {
  const { adapter, tenant } = ctx;
  const map = adapter[field.sourceMapPath] || {};
  if (tenant && map[tenant]) return map[tenant];
  return ["Job Board", "LinkedIn"];
}

// Which resume track is hanging off the form right now, read from the attachment's
// filename. Works because our own uploads are named Parth_Resume_<tag>.pdf, so every clone
// source we create from here on is self-describing.
//
// `{present: true, tag: null}` — a file is attached but we cannot tell which track — is
// deliberately NOT treated as a match. An unknown CV is exactly the case worth replacing:
// it is either somebody's older naming scheme or a file uploaded outside this tool, and a
// redundant re-upload is cheap next to shipping the wrong CV.
export function attachedTrackTag(ctx) {
  const { adapter } = ctx;
  const box = document.querySelector(adapter.experience.resume.container);
  // "No attachment" and "no attachment area to look in" are different facts and were
  // being collapsed into present:false. The step-2 shortcut below turns on that
  // difference, so it has to be reported: a visible-and-empty container is EVIDENCE of
  // nothing attached, whereas a missing container is merely the absence of evidence.
  if (!box) return { present: false, letter: null, text: "", container: false };
  const text = (box.innerText || "").trim();
  if (!text || !/\.pdf\b/i.test(text)) return { present: false, letter: null, text, container: true };
  const m = text.match(adapter.experience.resume.attachedNamePattern);
  return { present: true, letter: m ? m[1].toLowerCase() : null, text, container: true };
}

// The attachment names a LETTER; the track carries a tag. Compare on the initial, which is
// the only thing the two representations share.
export function attachmentMatchesTrack(attached, track) {
  if (!attached || !attached.present || !attached.letter) return false;
  const tag = track && track.tag ? String(track.tag).toLowerCase() : "";
  return !!tag && attached.letter === tag[0];
}

async function removeAttachment(ctx) {
  const { adapter } = ctx;
  const btn = document.querySelector(adapter.experience.resume.removeButton);
  if (!btn) return false;
  btn.click();
  // Gone when the container stops naming a PDF.
  for (let i = 0; i < 20; i++) {
    if (!attachedTrackTag(ctx).present) return true;
    await sleep(300);
  }
  return false;
}

// WHAT KIND OF CONTROL IS THIS. Order matters and is not cosmetic: a prompt tree contains
// a text input, so checking for an input first reports "text", and typing into it fills a
// SEARCH BOX and commits nothing — the field then reads as filled and the step bounces on
// Next. That exact trap already cost a round on "How Did You Hear About Us?".
function widgetKindOf(adapter, wrapper) {
  if (!wrapper) return null;
  if (wrapper.querySelector(adapter.dropdown.control)) return "listbox";
  if (wrapper.matches?.(adapter.promptTree.field) || wrapper.querySelector(adapter.promptTree.field)) {
    return "promptTree";
  }
  if (wrapper.querySelector("input,textarea")) return "text";
  return null;
}

// One education field, reconciled. Shape is detected rather than configured because these
// differ per tenant — a degree is a listbox on Adobe and free text elsewhere.
async function fillEducationField(ctx, report, key, sel, value, alternates = []) {
  const { adapter } = ctx;
  const label = `education.${key}`;
  if (value == null || value === "" || value === "FILL_ME") return;   // nothing to assert

  const wrapper = document.querySelector(sel);
  if (!wrapper) { report.push([label, "SKIPPED not on this form"]); return; }

  const kind = widgetKindOf(adapter, wrapper);
  if (!kind) { report.push([label, "FAILED unrecognised control"]); return; }

  const current = readFieldValue(ctx, { control: kind, selectors: [sel] });
  const d = reconcileDecision(kind, current, value, adapter);
  if (d.action !== "write") {
    report.push([label, d.action === "keep" ? `kept "${String(current).trim().slice(0, 30)}"`
                                            : `skipped: ${d.why}`]);
    return;
  }

  if (kind === "listbox") {
    const res = await pickListbox(ctx, sel, value);
    report.push([label, res.ok ? `set ${res.value}` : `FAILED ${res.reason}`]);
  } else if (kind === "promptTree") {
    // ONE attempt, and it scrolls. Measured on the live control: the list is a virtualised
    // alphabetical menu, and its input does NOT accept typed text — after typing, the box
    // still read empty and the rows were unfiltered. So there is nothing to search.
    //
    // A previous version tried searching first and falling back to this. That did not
    // merely fail, it CAUSED the failure: the search typed and erased into a dead input,
    // then the fallback clicked the opener again, which toggled the already-open panel
    // shut — so the walker reported "no row matched" against a list that was not there.
    // Two strategies against a stateful widget are not free.
    //
    // The candidate list is the profile's own wording plus any alternates the human wrote
    // down for it. These vocabularies are tenant-specific and often have no entry for a
    // perfectly ordinary answer — Adobe's 320-option Field of Study list contains no
    // "Computer Science" — but which substitute is TRUE is a fact about the applicant, so
    // it is read from the profile and never inferred here.
    const res = await pickPromptTree(ctx, sel, [[String(value), ...alternates]]);
    report.push([label, res.ok ? `set ${res.value}` : `FAILED ${res.reason}` +
      (res.near && res.near.length ? ` — closest of ${res.sawCount} options: ${res.near.join(", ")}`
                                   : res.sawCount ? ` (saw ${res.sawCount} options, none similar)` : "") +
      (res.widget ? ` ${JSON.stringify(res.widget)}` : "")]);
  } else {
    const el = wrapper.querySelector("input,textarea");
    setTextNative(el, String(value));
    report.push([label, "set"]);
  }
}

// Pick one value from a SEARCH-ONLY prompt: type the term, wait for results, click the
// match. Measured on the live Skills field — the panel opens showing "No Items." and
// populates only from the search box — so the scroll walker is not merely slow here, it
// has nothing to walk.
//
// Returns { ok } on success, { empty: true } when the search came back with nothing (a
// fact about the vocabulary, not a failure of ours), or { ok: false, reason }.
// WHICH RESULT MEANS WHAT WE ASKED FOR. Searching "python" returns twenty rows, and the
// plain word is not among them: the entry is "Python (Programming Language)", sitting
// beside "Python IDLE", "Maya Python", "Python Numpy" and "Python on z/OS". A prefix match
// would tick "Python Automation"; a substring match would tick "Maya Python". Both are
// different claims about someone's skills than the one they wrote down.
//
// So: the term itself, or the term with a parenthetical qualifier after it, and nothing
// else. Anything further from that is reported with what was on offer, so a real
// preference can be written into the profile instead of inferred here.
export function matchSearchResult(term, rows, txt) {
  const want = String(term).trim().toLowerCase();
  const of = (r) => txt(r).toLowerCase();
  return rows.find(r => of(r) === want)
      || rows.find(r => new RegExp(`^${want.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\(`).test(of(r)))
      || null;
}

export async function pickBySearch(ctx, wrapperSel, term) {
  const { adapter, rpc, bringIntoView } = ctx;
  const pt = adapter.promptTree;
  const wrap = document.querySelector(wrapperSel);
  if (!wrap) return { ok: false, reason: "field not found" };
  const input = wrap.querySelector("input");
  if (!input) return { ok: false, reason: "no search box" };

  bringIntoView(wrap);
  if (!isPanelOpen(wrap, input, pt)) {
    const r0 = input.getBoundingClientRect();
    if (!r0.width && !r0.height) return { ok: false, reason: "opener has no rect" };
    await rpc({ type: "CDP_CLICK",
                x: Math.round(r0.left + r0.width / 2), y: Math.round(r0.top + r0.height / 2) });
    await sleep(700);
  }

  // Clear whatever the last term left behind. Typing over it produced "Pythondata" when
  // done by hand, which searches for nothing and looks exactly like an empty vocabulary.
  if (input.value) await rpc({ type: "CDP_ERASE", times: input.value.length + 4 });
  await rpc({ type: "CDP_TYPE", text: term });

  // SUBMIT. The search is server-side and runs on Enter, not as you type. Waiting for
  // type-ahead results that never come reads exactly like a tenant with no vocabulary —
  // I measured that and drew that conclusion, and it was wrong.
  const submit = adapter.experience && adapter.experience.skillsSearchSubmit;
  if (submit) await rpc({ type: "CDP_KEY", key: submit });

  // Results arrive from the server, so wait for them to settle rather than reading once.
  let opts = [], prev = -1;
  for (let i = 0; i < 12; i++) {
    await sleep(300);
    if (input.value !== term) return { ok: false, reason: `search box holds "${input.value}"` };
    opts = allRows(pt);
    if (opts.length && opts.length === prev) break;
    if (!opts.length && showsEmptyState(pt) && i >= 3) return { empty: true, ok: false };
    prev = opts.length;
  }
  if (!opts.length) return { empty: true, ok: false };

  const txt = (o) => (o.innerText || "").trim();
  const hit = matchSearchResult(term, opts, txt);
  if (!hit) return { ok: false, reason: "no result means exactly this",
                     sample: opts.slice(0, 6).map(txt) };

  const target = clickPointFor(hit, pt, true);
  if (!target) return { ok: false, reason: "result row has no clickable box" };
  const blocker = whatIsAt(target.x, target.y, hit);
  if (blocker) return { ok: false, reason: `click point is covered by ${blocker}` };
  await rpc({ type: "CDP_CLICK", x: target.x, y: target.y });
  await sleep(900);
  return { ok: true, value: txt(hit) };
}

// How many values a multi-select currently holds, read from its own summary line. Used to
// confirm a pick actually landed rather than trusting the click.
export function selectedCount(wrap, pt) {
  const c = committedValue(wrap, pt);
  const m = c && String(c).match(/(\d+)\s+items?\s+selected/i);
  return m ? Number(m[1]) : (c ? 1 : 0);
}

// Skills — a multi-select over a fixed vocabulary, filled one value at a time.
//
// Every value is one the applicant listed on the resume being attached, resolved by
// stream so the form agrees with the PDF. Nothing here is inferred from a job description:
// adding a skill because the posting asked for it would be writing a claim about someone
// on the strength of what the employer wanted to hear.
//
// Whether it searches or scrolls is a per-tenant fact, so it is configured, not sniffed.
async function fillSkills(ctx, report, wanted) {
  const { adapter } = ctx;
  const exp = adapter.experience;
  const sel = exp.skills;
  if (!sel || !document.querySelector(sel)) return;      // not on this tenant's form
  const list = (wanted || []).map(s => String(s).trim()).filter(Boolean);
  if (!list.length) { report.push(["skills", "SKIPPED none in profile"]); return; }

  const wrap = document.querySelector(sel);
  const pt = adapter.promptTree;
  if (selectedCount(wrap, pt)) {                          // reconcile, as everywhere
    report.push(["skills", `kept ${committedValue(wrap, pt)}`]);
    return;
  }

  const cap = exp.maxSkills || 8;
  const giveUp = exp.skillsEmptyGiveUp || 2;
  const set = [], missing = [];
  let emptyRun = 0;

  for (const skill of list.slice(0, cap)) {
    const before = selectedCount(wrap, pt);
    const res = exp.skillsSearch ? await pickBySearch(ctx, sel, skill)
                                 : await pickPromptTree(ctx, sel, [[skill]]);

    if (res.empty) {
      missing.push(skill);
      // An empty vocabulary is a property of the TENANT, not of the term, so the third
      // miss in a row tells us nothing the second did not. Adobe returns "No Items." for
      // every term including a single letter.
      if (++emptyRun >= giveUp && !set.length) {
        report.push(["skills",
          `SKIPPED this tenant's skill list is empty — "${missing.join('", "')}" all returned nothing`]);
        return;
      }
      continue;
    }
    emptyRun = 0;

    // Verify against the field's own count. A click that highlights without committing is
    // the failure mode this widget specialises in.
    if (res.ok && selectedCount(wrap, pt) > before) { set.push(res.value || skill); continue; }
    missing.push(skill);
    if (!res.ok && res.reason && !set.length && missing.length >= giveUp) {
      report.push(["skills", `FAILED ${res.reason} — gave up after ${missing.join(", ")}`]);
      return;
    }
  }
  report.push(["skills", (set.length ? `set ${set.join(", ")}` : "set none") +
                         (missing.length ? ` (not offered: ${missing.join(", ")})` : "")]);
}

// Step 2. All of it is required: work history, education AND a resume.
// Filled from the STREAM-RESOLVED profile so the text matches the attached PDF.
async function fillMyExperience(ctx, report) {
  const { adapter, profile, dig, attachResume, track } = ctx;
  const exp = adapter.experience;
  const history = dig(profile, "work_history") || [];

  // MAKE THE ROWS FIRST, then fill them. Doing it inside the fill loop meant row 0 was
  // handled before any row existed — it was silently skipped, and everything shifted by
  // one. The parse may have supplied rows already (autofill) or none at all (manual), so
  // top up to one row per history entry either way.
  let rows = document.querySelectorAll(exp.work.jobTitle).length;
  if (rows) report.push(["work", `${rows} row(s) already on the form`]);
  while (rows < history.length) {
    const add = document.querySelector(exp.work.addAnother);
    if (!add) break;
    add.click();
    await sleep(1200);
    const now = document.querySelectorAll(exp.work.jobTitle).length;
    if (now <= rows) break;              // the button did not add one; stop rather than spin
    rows = now;
  }
  if (rows < history.length) {
    report.push(["work", `only ${rows} of ${history.length} row(s) could be created — ` +
                         `the rest of the history is NOT on this form`]);
  }

  for (let i = 0; i < Math.min(history.length, rows); i++) {
    const job = history[i];
    // Scope every lookup to THIS row. Falls back to positional indexing only if the row
    // container cannot be derived, which keeps the old behaviour for fields that do exist
    // once per row rather than failing outright.
    const rowEl = workRowScope(exp, i);
    const scope = (sel) => (rowEl ? rowEl.querySelector(sel) : nthWrapper(sel, i));

    // SETTLE "I currently work here" BEFORE ANYTHING ELSE IN THE ROW. Ticking it REMOVES
    // the End Date field from the DOM, so it decides whether a field the loop is about to
    // write even exists. It sits last in fieldMap, and object key order is far too fragile
    // a thing to hang a destructive DOM change on.
    //
    // It must also be able to UNTICK. The parse arrived with this ticked on a job that
    // ended years ago, and code that only ever ticks left it that way: End Date stayed
    // absent, reported as "field not present in this row", and the form claimed both jobs
    // were current. Reconciling in one direction only is not reconciling.
    const curWrap = scope(exp.work.currentlyWorkHere);
    if (curWrap) {
      const cb = curWrap.querySelector('input[type=checkbox]');
      const want = !!job.current;
      if (cb && cb.checked !== want) {
        cb.click();
        await sleep(900);
        report.push([`work[${i}].currentlyWorkHere`,
                     want ? "ticked" : "unticked — the parse had this job as current"]);
      } else if (cb) {
        report.push([`work[${i}].currentlyWorkHere`, `kept ${cb.checked ? "ticked" : "unticked"}`]);
      }
    }

    for (const [domKey, profKey] of Object.entries(exp.fieldMap)) {
      if (!(domKey in exp.work)) continue;
      if (domKey === "currentlyWorkHere") continue;   // settled above, on purpose
      const wrapper = scope(exp.work[domKey]);
      const val = job[profKey];
      if (!wrapper) {
        // Say so. A silently skipped field is how row 0 vanished from the last run
        // without a single line in the report to show for it.
        if (val != null && val !== "" && val !== "FILL_ME") {
          report.push([`work[${i}].${domKey}`, "FAILED field not present in this row"]);
        }
        continue;
      }

      if (domKey === "currentlyWorkHere") {
        const cb = wrapper.querySelector('input[type=checkbox]');
        // Tick BEFORE writing endDate: Workday deletes the End Date field outright when
        // this is checked, so the other order writes into a doomed node and the
        // "To is required" error never clears.
        if (cb && !!val && !cb.checked) { cb.click(); await sleep(900); }
        continue;
      }
      if (val == null || val === "" || val === "FILL_ME") continue;

      // Reconcile here too, and it matters MORE on this step than on step 1. Workday's
      // parse has usually already filled these, and rewriting a date is the single most
      // damaging thing this driver can do: a plain write displays without committing and
      // poisons the section, so a needless overwrite of a correct date turns a working
      // field into one that has to be cleared and retyped before it will validate.
      const isDate = domKey === "startDate" || domKey === "endDate";
      if (isDate && job.current && domKey === "endDate") continue;  // field is gone by design

      const current = readWrapperValue(ctx, wrapper, isDate ? "date" : "text");
      const d = reconcileDecision(isDate ? "date" : "text", current, val, adapter);
      if (d.action !== "write") {
        report.push([`work[${i}].${domKey}`,
                     d.action === "keep" ? `kept "${String(current).trim().slice(0, 30)}"`
                                         : `skipped: ${d.why}`]);
        continue;
      }

      if (isDate) {
        const res = await setDate(ctx, cssPathOf(wrapper), val);
        report.push([`work[${i}].${domKey}`, res.ok ? "set" : `FAILED ${res.reason}`]);
      } else {
        const el = wrapper.querySelector("input,textarea");
        if (el) { setTextNative(el, String(val)); report.push([`work[${i}].${domKey}`, "set"]); }
      }
    }
  }

  // Education. Three of the six configured fields were never filled — field of study and
  // both year boxes — and the three that were got written blind, overwriting whatever the
  // CV parse had already put there.
  const ed = exp.education;
  const year = (v) => { const m = String(v == null ? "" : v).match(/\b(19|20)\d{2}\b/); return m ? m[0] : null; };

  await fillEducationField(ctx, report, "school", ed.school, dig(profile, "education.school"));
  // degree_level matches the listbox options verbatim ("Bachelors", NOT "Bachelor's Degree").
  await fillEducationField(ctx, report, "degree", ed.degree, dig(profile, "education.degree_level"));
  await fillEducationField(ctx, report, "fieldOfStudy", ed.fieldOfStudy,
                           dig(profile, "education.major"),
                           dig(profile, "education.major_alternates") || []);
  await fillEducationField(ctx, report, "gpa", ed.gpa, dig(profile, "education.gpa"));
  // Only the graduation year is knowable from the profile. start_date is optional and
  // absent for most people, so a missing first year is reported rather than invented —
  // guessing it by subtracting a course length would be fabricating a fact about someone.
  await fillEducationField(ctx, report, "firstYear", ed.firstYear, year(dig(profile, "education.start_date")));
  await fillEducationField(ctx, report, "lastYear", ed.lastYear, year(dig(profile, "education.graduation_date")));

  // `profile` here is already stream-resolved, so skills.list is the attached CV's own set.
  await fillSkills(ctx, report, dig(profile, "skills.list"));

  // Resume — required. Reuses fill.js's DataTransfer attach so there is one implementation.
  //
  // Before uploading, check what is ALREADY attached. A clone brings the previous
  // application's PDF, and that is the one thing reconciling text cannot repair: every
  // answer on the form can be right while the wrong CV hangs off it, which is precisely
  // the contradiction the four tracks exist to avoid.
  const already = attachedTrackTag(ctx);
  if (attachmentMatchesTrack(already, track)) {
    report.push(["resume", `kept the attached ${track.tag} CV`]);
    return;
  }
  // The autofill route uploaded this track on the way in, and that upload is what built
  // this page — but it does NOT leave the file attached here. Confirmed on the live form:
  // this section was empty while the report claimed "already uploaded at entry", so the
  // application would have been submitted with no CV on it.
  //
  // The shortcut survives only for the case it was actually written for: a container we
  // cannot see at all, where "nothing attached" was never observed. If the section IS on
  // screen and empty, that is evidence, and evidence beats what we remember doing.
  if (!already.present && !already.container &&
      ctx.entryAttached && track && ctx.entryAttached === track.tag) {
    report.push(["resume", `uploaded at entry (${track.tag}); no attachment area on this step`]);
    return;
  }
  if (already.present) {
    // Replace, never add. A second upload may append rather than swap, and a form
    // carrying two CVs is worse than one carrying the wrong CV.
    const removed = await removeAttachment(ctx);
    report.push(["resume-replace",
                 `${already.letter ? `a "_${already.letter}" CV` : "an unrecognised file"} ` +
                 `was attached, need ${track && track.tag}` +
                 (removed ? " — removed it" : " — COULD NOT REMOVE IT, upload skipped")]);
    if (!removed) return;
  }

  const input = document.querySelector(exp.resume.input);
  if (input && attachResume) {
    try {
      const attached = await attachResume(input, track);
      await sleep(1500);
      // Workday renders "<name>|<size>|Successfully Uploaded!" — assert on that rather
      // than trusting the change event, which fires whether or not the upload took.
      const box = document.querySelector(exp.resume.container);
      const ok = attached && box && exp.resume.successText.test(box.innerText || "");
      report.push(["resume", ok ? `attached (${track && track.tag})`
                                : `FAILED ${attached ? "no success text" : "input rejected the file"}`]);
    } catch (e) {
      report.push(["resume", `FAILED ${e.message}`]);
    }
  } else {
    report.push(["resume", "FAILED file input not found"]);
  }
}

// Steps 3-5: opaque hex ids, so pure taxonomy work — classify the label, answer it.
// Anything the taxonomy will not answer deterministically is left for the human rather
// than guessed; these are attestations and EEO, where a wrong auto-answer is a false
// statement on a real application.
// ── derived answers ──────────────────────────────────────────────────────────
// Some questions have a correct answer that no static profile field can hold, because it
// depends on WHICH company is asking. "Have you been employed by Adobe in the past?" is
// the one that turns up on every Workday tenant, and it is required.
//
// The taxonomy cannot express this: a deterministic rule resolves a fixed dotted path, so
// the only way to answer it there is a hardcoded "No" — which is a false statement about a
// person the day he applies to a former employer, and nothing would ever catch it. Derive
// it from work_history instead, which is the only honest source.

// Company names never match exactly across systems: "Adobe" against "Adobe Inc.", a tenant
// slug against a display name. Strip to comparable cores.
const COMPANY_NOISE = /\b(inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|sa|nv|group|holdings|technologies|technology|labs|the)\b/g;
export function companyCore(s) {
  return String(s == null ? "" : s).toLowerCase()
    .replace(/[.,&'’()]/g, " ").replace(COMPANY_NOISE, " ")
    .replace(/\s+/g, " ").trim();
}

export function sameCompany(a, b) {
  const x = companyCore(a), y = companyCore(b);
  if (!x || !y) return false;
  if (x === y) return true;
  // Token containment, not substring: "adobe" must not match "adobe systems research" by
  // accident of letters, but should match it by words. Guards against short names like
  // "co" or "ai" swallowing everything.
  const xs = x.split(" ").filter(Boolean), ys = y.split(" ").filter(Boolean);
  if (!xs.length || !ys.length) return false;
  const shorter = xs.length <= ys.length ? xs : ys, longer = shorter === xs ? ys : xs;
  return shorter.every(t => longer.includes(t));
}

// "Have you worked here before?" answered from work history. Returns null when it cannot
// be decided, so the question falls through to being ASKED rather than guessed at.
export function priorEmployerAnswer(ctx) {
  const { profile, dig, company, tenant } = ctx;
  const asking = String(company || tenant || "").trim();
  if (!asking) return null;
  const history = dig(profile, "work_history") || [];
  if (!Array.isArray(history) || !history.length) return null;
  const worked = history.some(e => e && sameCompany(e.employer, asking));
  return worked ? "Yes" : "No";
}

const DERIVED = { priorEmployerAnswer };

async function fillQuestions(ctx, report, needsInput) {
  const { adapter, profile, tax, classify, render, company } = ctx;
  const wrappers = [...document.querySelectorAll('[data-automation-id^="formField-"]')];
  // Steps 1 and 2 mix tenant questions in with the core fields this driver already set
  // by hand. Re-classifying those would re-answer a solved field from a label match —
  // so skip the known core ids and leave everything else to the taxonomy.
  const known = new Set(adapter.customQuestion.knownCoreIds || []);

  for (const w of wrappers) {
    const aid = w.getAttribute("data-automation-id");
    if (known.has(aid)) continue;

    // A FILE UPLOAD IS NOT A QUESTION. The attach path owns it, and no answer exists that
    // could satisfy it. Live, the resume widget came through here as an unanswered
    // question — label "Upload either DOC, DOCX, HTML, PDF, or TXT file types (5MB
    // max)Parth_Resume_d.pdf loading..." — and paused the whole run for input on a field
    // that had just been filled successfully.
    //
    // Testing for input[type=file] alone was NOT enough, which is how it survived a first
    // fix: once the upload completes Workday swaps the input for a file chip, so by the
    // time the taxonomy pass runs there is no file input left to find. Match the upload
    // container as well, and fall back to the label — the prompt text is unmistakable and
    // is the one signal that outlives the widget's own DOM.
    const cq = adapter.customQuestion;
    if ((cq.skipContaining || []).some(s => w.matches?.(s) || w.querySelector(s))) continue;

    const label = extractLabel(w, adapter);
    if (!label) continue;
    if (cq.skipLabelPattern && cq.skipLabelPattern.test(label)) continue;

    // Derived answers run BEFORE the taxonomy, because the taxonomy structurally cannot
    // produce them — and after the skips, so they never fire on a widget that is not a
    // question. A null answer means undecidable and falls through to being asked.
    const rule = (adapter.derivedQuestions || []).find(r => r.pattern.test(label));
    if (rule) {
      const answer = (DERIVED[rule.resolve] || (() => null))(ctx);
      if (answer != null) {
        const hit = clickChoiceByText(w, answer);
        report.push([label.slice(0, 45), hit ? `set ${answer} (derived)`
                                             : `FAILED no "${answer}" option`]);
        if (!hit) needsInput.push({ aid, label, bucket: "choice-miss" });
        continue;
      }
      // Undecidable: say so in the report, then let it be asked like anything else.
      report.push([label.slice(0, 45), "could not derive — asking"]);
    }

    const c = classify(label, company, profile, tax);
    if (c.bucket !== "deterministic" || !c.filled) {
      needsInput.push({ aid, label, bucket: c.bucket || "unmatched" });
      continue;
    }
    const value = render(c.value);

    if (w.querySelector(adapter.dropdown.control)) {
      const res = await pickListbox(ctx, `[data-automation-id="${aid}"]`, value);
      report.push([label.slice(0, 45), res.ok ? `set ${res.value}` : `FAILED ${res.reason}`]);
    } else if (w.querySelector('input[type=radio],input[type=checkbox]')) {
      const ok = clickChoiceByText(w, value);
      report.push([label.slice(0, 45), ok ? `set ${value}` : "FAILED no matching option"]);
      if (!ok) needsInput.push({ aid, label, bucket: "choice-miss" });
    } else {
      const el = w.querySelector("input,textarea");
      if (el) { setTextNative(el, value); report.push([label.slice(0, 45), "set"]); }
    }
  }
}

// ── small helpers ────────────────────────────────────────────────────────────

function extractLabel(wrapper, adapter) {
  const el = wrapper.querySelector("label") || wrapper.querySelector("legend");
  let t = el ? el.innerText : (wrapper.getAttribute("aria-label") || "");
  if (!t) t = (wrapper.innerText || "").split("\n")[0];
  return (t || "").replace(adapter.labelStripPattern, "").trim();
}

// Radio/checkbox option ids are opaque and unstable ("eeap3"/"eeap4"), so match on the
// associated label's TEXT, never on id or index.
function clickChoiceByText(wrapper, value) {
  const want = String(value).trim().toLowerCase();
  const inputs = [...wrapper.querySelectorAll('input[type=radio],input[type=checkbox]')];
  for (const inp of inputs) {
    const lab = inp.id ? wrapper.querySelector(`label[for="${CSS.escape(inp.id)}"]`) : null;
    const text = (lab ? lab.innerText : (inp.closest("label") || {}).innerText || "").trim();
    if (text.toLowerCase() === want) { (lab || inp).click(); return true; }
  }
  return false;
}

// Repeating sections reuse the same automation-id per row, so index into them rather than
// trusting the row's own id (Workday's carries an opaque counter: workExperience-11--...).
// THE ROW, not the i-th node of one field type. Indexing each field independently across
// the whole page only works if every row has every field, and they do not: ticking "I
// currently work here" REMOVES that row's End Date from the DOM. With a current first job
// and an ended second one, the page holds two Job Title wrappers but only ONE End Date —
// the second row's, sitting at index 0. Asking for index 1 found nothing and reported "field
// not present in this row" for a field that was on screen and empty.
//
// Derived by climbing from the row's Job Title until the ancestor would swallow a second
// row, so it needs no new selector for a container whose automation-id carries an opaque
// counter (workExperience-11--...).
function workRowScope(exp, i) {
  const title = [...document.querySelectorAll(exp.work.jobTitle)][i];
  if (!title) return null;
  let node = title;
  while (node.parentElement &&
         node.parentElement.querySelectorAll(exp.work.jobTitle).length === 1) {
    node = node.parentElement;
  }
  return node;
}

// STRICT. This used to fall back to all[0] when index i did not exist, which is how the
// second job got written into the first row: with no rows on arrival, work[0] found
// nothing and was skipped, then work[1] added a row, asked for index 1, and was handed
// index 0 — leaving the form claiming an old job was the current one. Handing back a
// DIFFERENT row than the one asked for is far worse than handing back nothing, because
// nothing is reported and a wrong row is not.
function nthWrapper(selector, i) {
  return [...document.querySelectorAll(selector)][i] || null;
}

// setDate takes a selector; give it a stable one for an element we already hold.
let _tmpId = 0;
function cssPathOf(el) {
  if (!el.id) el.id = `wd-drv-${++_tmpId}`;
  return `#${CSS.escape(el.id)}`;
}

// ── reconcile: keep what Workday already got right ───────────────────────────
// Both entry routes arrive at a form that is already mostly filled — a clone replays a
// past application, the autofill route parses the PDF. So the job here is not to type
// everything, it is to leave alone what matches the chosen resume track and overwrite only
// what is missing or wrong.
//
// This matters beyond tidiness. Rewriting a date field is the single most dangerous thing
// this driver does: a synthesized write displays but never commits AND poisons the
// section, so the recovery is a clear-then-trusted-retype. A reconcile that overwrites
// needlessly walks into that on every run, on a field that was already correct.

const digitsOf = (s) => String(s == null ? "" : s).replace(/\D+/g, "");
const tidy = (s) => String(s == null ? "" : s).trim().replace(/\s+/g, " ").toLowerCase();

// Month/year in whatever shape it came from: "2024-01" from the profile, "01/2024" off the
// spinners. Compared as MMYYYY so the two representations meet.
function toMonthYear(v) {
  const s = String(v == null ? "" : v).trim();
  const iso = s.match(/^(\d{4})-(\d{1,2})\b/);
  if (iso) return String(iso[2]).padStart(2, "0") + iso[1];
  const my = s.match(/^(\d{1,2})\D+(\d{4})$/);
  if (my) return String(my[1]).padStart(2, "0") + my[2];
  const raw = digitsOf(s);
  return raw.length === 6 ? raw : null;
}

// Is the field empty? Not the same as an empty string: a listbox reading "Select One" and
// a prompt tree with no "1 item selected" badge are both unset while carrying text.
export function isBlankValue(kind, current, adapter) {
  const c = String(current == null ? "" : current).trim();
  if (!c) return true;
  if (kind === "listbox") return adapter.dropdown.placeholderText.test(c);
  if (kind === "promptTree") return !adapter.promptTree.committedText.test(c);
  return false;
}

// Does what the form holds already say what we mean? Deliberately generous in the places
// the form legitimately reformats, and strict everywhere else — a false "same" leaves a
// wrong answer on a real application, which is worse than a redundant write everywhere
// except dates.
export function sameValue(kind, current, intended, adapter) {
  const a = tidy(current), b = tidy(intended);
  if (!a || !b) return false;
  if (a === b) return true;

  if (kind === "phone") {
    // Workday reformats on blur ("+1 555-123-4567" -> "(555) 123-4567") and strips the
    // country code into its own control, so only the national number can be compared.
    const da = digitsOf(a), db = digitsOf(b);
    return da.length >= 7 && db.length >= 7 && da.slice(-10) === db.slice(-10);
  }
  if (kind === "date") {
    const ma = toMonthYear(a), mb = toMonthYear(b);
    return !!ma && ma === mb;
  }
  if (kind === "listbox" || kind === "promptTree") {
    // Option text is the form's wording, not ours: "Bachelor's Degree" against
    // "Bachelor's Degree (BA/BS)". Re-picking a dropdown costs a menu round-trip and, on
    // a prompt tree, a pair of trusted clicks, so containment either way counts as agreed.
    return a.includes(b) || b.includes(a);
  }
  return false;
}

// keep / write / skip, and why. Pure — this is the whole reconcile rule.
export function reconcileDecision(kind, current, intended, adapter) {
  const want = String(intended == null ? "" : intended).trim();
  // FILL_ME is the profile's "not set yet" marker, never a value to type onto a form.
  if (!want || want === "FILL_ME") {
    return { action: "skip", why: "nothing in the profile to assert" };
  }
  if (isBlankValue(kind, current, adapter)) return { action: "write", why: "was empty" };
  if (sameValue(kind, current, intended, adapter)) {
    return { action: "keep", why: "already matches" };
  }
  return { action: "write", why: `differs (form had "${String(current).trim()}")` };
}

// Same idea as readFieldValue, for a wrapper we already hold rather than a configured
// field — the repeating work-history rows are found by index, not by selector.
function readWrapperValue(ctx, wrapper, kind) {
  const { adapter } = ctx;
  if (!wrapper) return null;
  if (kind === "date") {
    const mm = wrapper.querySelector(adapter.date.month);
    const yy = wrapper.querySelector(adapter.date.year);
    if (!mm || !yy || !mm.value || !yy.value) return null;
    return `${mm.value}/${yy.value}`;
  }
  const el = wrapper.querySelector("input,textarea");
  return el ? el.value : null;
}

// What the form currently holds for one configured field. null means "cannot tell", which
// callers must treat as blank — writing over an unknown is recoverable, trusting one is not.
function readFieldValue(ctx, f) {
  const { adapter } = ctx;
  const wrapper = f.selectors[0].replace(/\s+button.*$/, "");
  if (f.control === "listbox") {
    const btn = document.querySelector(`${wrapper} ${adapter.dropdown.control}`);
    return btn ? (btn.innerText || "").trim() : null;
  }
  if (f.control === "promptTree") {
    const w = document.querySelector(wrapper);
    if (!w) return null;
    const chosen = [...w.querySelectorAll(adapter.promptTree.selected)]
      .map(n => (n.innerText || "").trim()).filter(Boolean);
    return chosen.length ? chosen.join(", ") : (w.innerText || "").trim();
  }
  if (f.control === "date") {
    const w = document.querySelector(wrapper);
    if (!w) return null;
    const mm = w.querySelector(adapter.date.month), yy = w.querySelector(adapter.date.year);
    if (!mm || !yy || !mm.value || !yy.value) return null;
    return `${mm.value}/${yy.value}`;
  }
  const el = fieldInput(f.selectors[0]) || fieldInput(wrapper);
  return el ? el.value : null;
}

// ── entry: advert -> open form ───────────────────────────────────────────────
// A Workday job page is an advert with no form on it. Apply opens a modal offering up to
// four ways to start, and the form only exists once one is chosen. Everything below gets
// from the advert to step 1; runWorkday takes over there.

// WHICH ROUTE. Pure on purpose: this is where a silent wrong answer is expensive and
// invisible — every route lands on the same six steps, so picking badly does not throw,
// it just fills slower or replays an application it should not have.
//
// `preferred` comes from applied.json via the bridge and wins whenever Workday actually
// offers it. `available` is ground truth about what is clickable right now: a tenant with
// no history simply has no "use my last application" button, and a route we cannot click
// is not a route.
export function chooseRoute(preferred, available, order) {
  const avail = new Set(available || []);
  const ladder = [];
  for (const r of [preferred, ...(order || [])]) if (r && !ladder.includes(r)) ladder.push(r);

  const route = ladder.find(r => avail.has(r)) || null;
  const out = { route, preferred: preferred || null, available: [...avail] };

  if (!route) { out.reason = "the modal offered no route we know how to take"; return out; }
  if (route === preferred) out.reason = `took the decided route '${route}'`;
  else out.reason = `'${preferred}' was not offered; fell back to '${route}'`;

  // Our records said cold, Workday says otherwise — he applied to this tenant outside the
  // tool, or a submit never got logged. Worth surfacing rather than silently ignoring: it
  // means applied.json is incomplete, and a clone we did not record has an unknown track,
  // which is exactly the case the reconcile pass has to catch rather than trust.
  if (preferred !== "useMyLastApplication" && avail.has("useMyLastApplication")) {
    out.unrecordedHistory = true;
  }
  return out;
}

// Which of the adapter's routes the modal is actually showing.
function offeredRoutes(adapter) {
  const routes = adapter.entry.routes;
  return Object.keys(routes).filter(k => {
    const el = document.querySelector(routes[k]);
    return !!el && el.getAttribute("aria-disabled") !== "true" && !el.disabled;
  });
}

// WHICH step this is, by name. The number is not stable: the autofill route counts its
// upload page as step 1, so the same My Information page is step 1 on one flow and step 2
// of 7 on another. Names are matched loosely because tenants reword them slightly.
export function stepKind(adapter, step) {
  const name = String((step && step.name) || "").toLowerCase().replace(/[^a-z]+/g, "");
  if (!name) return null;
  for (const [kind, aliases] of Object.entries(adapter.multiStep.stepKinds || {})) {
    if (aliases.some(a => name.includes(String(a).toLowerCase().replace(/[^a-z]+/g, "")))) {
      return kind;
    }
  }
  return null;
}

// Are we on the page whose footer button SUBMITS? Derived from the progress bar's own
// "step N of M" rather than a hardcoded number, because the step count varies with the
// entry route — this flow reported 7 steps, and a hardcoded 6 both stops one page early
// and, on any flow with fewer steps, would be a guess about where Submit lives. The
// adapter's submitStep stays as a floor so a missing/garbled total cannot open the gate.
export function isFinalStep(adapter, step) {
  if (!step) return true;                       // unknown position: never advance
  if (Number.isFinite(step.of) && step.of > 0) return step.n >= step.of;
  return step.n >= adapter.multiStep.submitStep;
}

// Wait for the step's page to actually exist before touching it. The progress bar moves
// first, so without this the fillers run against an empty document and report every
// control as missing — which reads as a pile of broken selectors rather than one early
// read. Falls back to "any form field has rendered" for the steps with no known container.
async function awaitStepReady(ctx, kind, timeoutMs = 20000) {
  const { adapter } = ctx;
  const marker = (adapter.multiStep.stepPages || {})[kind];
  if (marker && await waitFor(marker, timeoutMs)) return true;
  const any = await waitFor('[data-automation-id^="formField-"]', marker ? 4000 : timeoutMs);
  return !!any;
}

// A real wizard step, not a route page wearing the same progress bar. currentStep already
// rejects the entry names, so this waits on the thing we actually mean.
async function waitForWizard(ctx, timeoutMs = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (currentStep(ctx.adapter)) return true;
    await sleep(400);
  }
  return false;
}

// Present AND actually clickable. Workday renders its footer button before the page is
// ready and disables it, so existence alone is not permission to click.
export function isEnabled(el) {
  if (!el) return false;
  if (el.disabled) return false;
  const aria = el.getAttribute && el.getAttribute("aria-disabled");
  return aria !== "true";
}

async function waitForEnabled(sel, timeoutMs, step = 300) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const el = document.querySelector(sel);
    if (isEnabled(el)) return el;
    await sleep(step);
  }
  return null;
}

async function waitFor(sel, timeoutMs, step = 300) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const el = document.querySelector(sel);
    if (el) return el;
    await sleep(step);
  }
  return null;
}

export function needsSignIn(adapter) {
  const s = adapter.entry.signIn;
  if (!s || !Array.isArray(s.markers)) return false;
  // Only meaningful when there is no Apply button — a signed-in advert can still carry a
  // "sign in" link in its header, and treating that as a wall would stop every run.
  if (document.querySelector(adapter.entry.applyButton)) return false;
  return s.markers.some(m => document.querySelector(m));
}

// The autofill route wants the PDF before it will build anything. Attach the track's file
// through the SAME helper the other three adapters use, then wait for the parse: it
// rebuilds the form when it lands, so anything written before that is discarded.
async function uploadAndSettle(ctx, entry) {
  const { adapter, attachResume, track } = ctx;
  const up = adapter.entry.resumeUpload;

  const input = await waitFor(up.input, 12000);
  if (!input) {
    return { ...entry, ok: false, stage: "resume-upload",
             error: "the autofill route never showed a file input" };
  }
  try {
    await attachResume(input, track);
  } catch (err) {
    return { ...entry, ok: false, stage: "resume-upload",
             error: `could not attach the ${track && track.tag} resume: ${err && err.message || err}` };
  }
  entry.attached = track && track.tag;

  // Workday enables Next once it has accepted and parsed the file. That is the signal —
  // the wizard's own fields do not exist yet and cannot be waited on from here.
  const go = await waitForEnabled(up.continueButton, up.settleTimeoutMs);
  if (!go) {
    return { ...entry, ok: false, stage: "resume-parse",
             error: "the resume was attached but Next never became clickable — either the " +
                    "parse failed or the page wants something else first" };
  }
  // Safe to click: this is the entry page, not step 6, so the footer button advances
  // rather than submits. The step guard in the main loop covers the wizard itself.
  go.click();
  return { ...entry, ok: true };
}

// Get from the advert to step 1. Returns {ok, route, ...} and never types into the form.
export async function enterApplyFlow(ctx) {
  const { adapter, rpc, track } = ctx;
  const e = adapter.entry;

  // Already inside the wizard (he clicked Apply himself, or a re-fill). Nothing to do —
  // and clicking Apply again from in here would be a different button entirely.
  if (currentStep(adapter)) return { ok: true, route: "already-in-flow", entered: false };

  // PARKED ON A ROUTE PAGE. The modal navigates to /apply/<route>, so a second Fill starts
  // here, not on the advert — and there is no Apply button to click any more. Resume from
  // wherever that route got to rather than reporting the missing button, which is what
  // happened live and read like a broken selector.
  const parked = currentRoutePage(adapter);
  if (parked) {
    const entry = { ok: true, route: parked, entered: true, resumed: true,
                    why: `resumed on the '${parked}' page` };
    if (parked === "autofillWithResume") {
      const up = await uploadAndSettle(ctx, entry);
      if (!up.ok) return up;
      Object.assign(entry, up);
    }
    const arrived = await waitForWizard(ctx);
    return arrived ? entry
                   : { ...entry, ok: false, stage: "wizard",
                       error: `parked on the '${parked}' page and no step marker appeared` };
  }

  if (needsSignIn(adapter)) {
    return { ok: false, stage: "signin",
             error: "this tenant wants an account before it will show the form. Sign in " +
                    "on the tab, then press Fill again — signing in is deliberately not " +
                    "automated." };
  }

  const apply = document.querySelector(e.applyButton);
  if (!apply) {
    return { ok: false, stage: "apply-button",
             error: "no Apply button on this page. If the form is already open this is a " +
                    "step-detection problem, not an entry one." };
  }
  apply.click();

  const modal = await waitFor(e.modal, 12000);
  if (!modal) {
    return { ok: false, stage: "modal",
             error: "clicked Apply but the route modal never appeared" };
  }

  // Ask Python which route our own records justify. Best-effort: a bridge that is down
  // must not stop an application, so an unanswered call falls back to the adapter's
  // default and the ladder below still constrains it to something clickable.
  // Bounded. background's bridge round-trip has no timeout of its own, so a bridge that is
  // down or mid-reconnect leaves this promise unsettled — and an entry step that never
  // returns looks exactly like a hung fill, with no report written to say why. The route
  // is an optimisation; the ladder below still constrains us to something clickable.
  let decided = null;
  try {
    const res = await Promise.race([
      rpc({ type: "WORKDAY_ROUTE", url: location.href }),
      sleep(6000).then(() => null),
    ]);
    decided = res && res.route ? res : null;
  } catch (_) { /* bridge off — fall through */ }

  const pick = chooseRoute(decided ? decided.route : e.fallbackRoute,
                           offeredRoutes(adapter), e.routePreference);
  if (!pick.route) {
    return { ok: false, stage: "route", error: pick.reason, offered: pick.available };
  }

  document.querySelector(e.routes[pick.route]).click();

  const entry = { ok: true, route: pick.route, entered: true, why: pick.reason,
                  tenant: decided ? decided.tenant : null,
                  unrecordedHistory: !!pick.unrecordedHistory };

  if (pick.route === "autofillWithResume") {
    const up = await uploadAndSettle(ctx, entry);
    if (!up.ok) return up;
  }

  // Both routes end in the wizard.
  if (await waitForWizard(ctx)) return entry;
  return { ...entry, ok: false, stage: "wizard",
           error: `took the '${pick.route}' route but no step marker ever appeared` };
}

// ── the loop ─────────────────────────────────────────────────────────────────

export async function runWorkday(ctx) {
  const { adapter, rpc } = ctx;
  const report = [];
  const needsInput = [];
  const steps = [];

  // Advert -> open form. A no-op when the form is already open, so pressing Fill on a
  // half-finished application still works. Done BEFORE the debugger attaches: entry is all
  // ordinary clicks, and a sign-in wall should not leave a debugging banner on the tab.
  const entry = await enterApplyFlow(ctx);
  if (!entry.ok) {
    return { ok: false, adapter: "workday", stage: entry.stage, error: entry.error,
             entry, report };
  }

  // Step 2 needs to know the entry route already uploaded the CV, or it re-uploads and the
  // input rejects the duplicate.
  ctx.entryAttached = entry.attached || null;

  // Into the report array rather than onto one return, so HOW the application started
  // survives every exit below — it is the first thing worth knowing when a run misbehaves,
  // and a clone that went wrong looks nothing like an autofill that went wrong.
  if (entry.entered) {
    report.push(["entry", entry.attached ? `${entry.route} (${entry.attached} resume)`
                                         : entry.route]);
    if (entry.unrecordedHistory) {
      report.push(["entry-note",
                   "Workday offered a previous application we have no record of — " +
                   "applied.json is incomplete for this tenant"]);
    }
  }

  const att = await rpc({ type: "CDP_ATTACH" });
  if (!att || !att.ok) {
    return { ok: false, adapter: "workday",
             error: "could not attach the debugger — Workday's prompt trees and date " +
                    "fields need trusted clicks. Close DevTools on this tab and retry." };
  }

  try {
    let guard = 0;
    while (guard++ < 10) {
      const step = currentStep(adapter);
      if (!step) return { ok: false, adapter: "workday", error: "no progress bar — not on an apply flow page", report };

      // Never advance off Review: on step 6 the footer button IS submit.
      if (isFinalStep(adapter, step)) {
        steps.push({ n: step.n, name: step.name, action: "stopped" });
        return { ok: true, adapter: "workday", stoppedAt: step, report, needsInput, steps,
                 note: "Reached Review. Submitting is deliberately left to you." };
      }

      const before = needsInput.length;
      // DISPATCH ON THE NAME, NEVER THE NUMBER. Verified live: the autofill flow counts
      // its upload page as step 1, so My Information is step 2 of 7 — and dispatching on
      // the number ran the work-history filler against the My Information page, which
      // reported three missing selectors that were all simply on a different page.
      // The number depends on the entry route; the name does not.
      const which = stepKind(adapter, step);
      if (!await awaitStepReady(ctx, which)) {
        report.push([`step${step.n}`, `"${step.name}" never rendered any fields`]);
      }
      if (which === "myInformation") await fillMyInformation(ctx, report);
      else if (which === "myExperience") await fillMyExperience(ctx, report);
      // Steps 1 and 2 also carry tenant questions (Adobe puts a required "Have you been
      // employed by Adobe in the past?" on step 1), so the taxonomy pass runs on EVERY
      // step, not just 3-5. knownCoreIds keeps it off the fields already set above.
      await fillQuestions(ctx, report, needsInput);

      // An unanswered question is usually a REQUIRED one, so Next would just bounce off
      // server-side validation. Stop and surface it rather than looping into the wall.
      if (needsInput.length > before) {
        steps.push({ n: step.n, name: step.name, action: "paused for input" });
        return { ok: true, adapter: "workday", pausedAt: step, report, needsInput, steps,
                 note: `${needsInput.length - before} question(s) on "${step.name}" need ` +
                       `you before this step can advance.` };
      }

      const next = document.querySelector(adapter.multiStep.nextButton);
      if (!next) return { ok: false, adapter: "workday", error: "no Next button", report, steps };
      next.click();

      const after = await waitForStepChange(adapter, step.n);
      const errs = stepErrors(adapter);
      if (!after || after.n === step.n) {
        // Server-side validation rejected the step. The errors name the fields.
        steps.push({ n: step.n, name: step.name, action: "blocked" });
        return { ok: false, adapter: "workday", blockedAt: step, errors: errs,
                 report, needsInput, steps };
      }
      steps.push({ n: step.n, name: step.name, action: "advanced" });
    }
    return { ok: false, adapter: "workday", error: "step loop guard tripped", report, steps };
  } finally {
    await rpc({ type: "CDP_DETACH" });
  }
}

export default runWorkday;
