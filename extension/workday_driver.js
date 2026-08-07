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
  return { n: Number(m[1]), of: Number(m[2]),
           name: (el.innerText || "").split("\n").pop().trim() };
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

  btn.click();
  await sleep(700);

  const boxes = [...document.querySelectorAll(dd.optionScope)];
  let opts = [];
  for (const b of boxes) opts.push(...b.querySelectorAll(dd.option));
  opts = opts.filter(o => !o.matches(dd.excludeOption) &&
                          !dd.placeholderText.test((o.innerText || "").trim()));

  const want = String(wanted).trim().toLowerCase();
  const txt = (o) => (o.innerText || "").trim();
  const hit = opts.find(o => txt(o).toLowerCase() === want)
           || opts.find(o => txt(o).toLowerCase().startsWith(want))
           || opts.find(o => txt(o).toLowerCase().includes(want));

  if (!hit) {
    btn.click();                                   // close, leave the field untouched
    return { ok: false, reason: "no option matched",
             wanted, sample: opts.slice(0, 8).map(txt) };
  }
  hit.click();
  await sleep(600);
  return { ok: true, value: (btn.innerText || "").trim() };
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
  await rpc({ type: "CDP_CLICK",
              x: Math.round(r0.left + r0.width / 2), y: Math.round(r0.top + r0.height / 2) });
  await sleep(900);

  const rows = () => [...document.querySelectorAll(`${pt.portal} ${pt.row}`)];
  const rowText = (el) => (el.innerText || "").trim();

  for (let depth = 0; depth < path.length; depth++) {
    const want = String(path[depth]).trim().toLowerCase();
    const isLeaf = depth === path.length - 1;

    // The list is virtualized: only rendered rows exist in the DOM, so a target further
    // down needs scrolling before it can be found at all.
    let hit = null;
    for (let scroll = 0; scroll < 12; scroll++) {
      hit = rows().find(x => rowText(x).toLowerCase() === want)
         || rows().find(x => rowText(x).toLowerCase().startsWith(want));
      if (hit) break;
      const list = document.querySelector(pt.portal);
      if (!list) break;
      const before = list.scrollTop;
      list.scrollTop += list.clientHeight || 200;
      await sleep(250);
      if (list.scrollTop === before) break;         // bottom reached
    }
    if (!hit) {
      return { ok: false, reason: `no row matched at depth ${depth}`,
               wanted: path[depth], sample: rows().slice(0, 8).map(rowText) };
    }

    const r = hit.getBoundingClientRect();
    // Leaves commit only when the RADIO is hit; category rows drill in from anywhere.
    const x = isLeaf ? Math.round(r.left + (pt.radioOffsetFromLeft ?? 22))
                     : Math.round(r.left + r.width / 2);
    await rpc({ type: "CDP_CLICK", x, y: Math.round(r.top + r.height / 2) });
    await sleep(1100);
  }

  const committed = wrap.querySelector(pt.selected);
  return committed
    ? { ok: true, value: (committed.innerText || "").trim() }
    : { ok: false, reason: "clicked through but nothing committed" };
}

// Split month/year spinner.
//
// THE TRAP, and it cost the most to find: a synthesized native-setter write DISPLAYS the
// value — the field read back "current value is 1/2024" and looked right on screen — but
// never reaches Workday's validation store, so Next bounces with "The field From is
// required" while showing 01/2024. Worse, that write POISONS the section: typing over it
// with real keystrokes did not clear the error either. It has to be erased first, then
// typed with trusted keys. Never setNativeValue a Workday date.
export async function setDate(ctx, wrapperSel, iso) {
  const { adapter, rpc, bringIntoView } = ctx;
  const d = adapter.date;
  const wrap = document.querySelector(wrapperSel);
  if (!wrap) return { ok: false, reason: "date field not found" };
  const m = String(iso || "").match(/^(\d{4})-(\d{1,2})/);
  if (!m) return { ok: false, reason: `unparseable date ${JSON.stringify(iso)} (want YYYY-MM)` };
  const digits = String(m[2]).padStart(2, "0") + m[1];   // MMYYYY

  const month = wrap.querySelector(d.month);
  if (!month) return { ok: false, reason: "no month section" };
  bringIntoView(wrap);
  const r = month.getBoundingClientRect();
  await rpc({ type: "CDP_CLICK",
              x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) });
  await sleep(250);
  await rpc({ type: "CDP_ERASE", times: d.clearKeystrokes || 8 });
  await sleep(200);
  await rpc({ type: "CDP_TYPE", text: digits });        // auto-advances month -> year
  await sleep(500);
  return { ok: true, value: digits };
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

  const r = await pickListbox(ctx, `[data-automation-id="${pre.field}"]`, country);
  report.push(["country", r.ok ? `set ${r.value}` : `FAILED ${r.reason}`]);
  if (!r.ok) return;

  for (let i = 0; i < 20 && !document.querySelector(pre.thenWaitFor); i++) await sleep(300);
  if (!document.querySelector(pre.thenWaitFor)) {
    report.push(["step1", "FAILED form did not expand after country"]);
    return;
  }

  for (const f of adapter.coreFields) {
    if (f.skip) continue;
    let value = f.value != null ? f.value : (f.profilePath ? dig(profile, f.profilePath) : null);
    if (value == null || value === "" || value === "FILL_ME") {
      if (!f.optional) report.push([f.key, "SKIPPED no value in profile"]);
      continue;
    }
    if (f.transform === "stripCountryCode") value = String(value).replace(/^\+\d+\s*/, "");

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

// Step 2. All of it is required: work history, education AND a resume.
// Filled from the STREAM-RESOLVED profile so the text matches the attached PDF.
async function fillMyExperience(ctx, report) {
  const { adapter, profile, dig, attachResume, track } = ctx;
  const exp = adapter.experience;
  const history = dig(profile, "work_history") || [];

  for (let i = 0; i < history.length; i++) {
    const job = history[i];
    if (i > 0) {
      const add = document.querySelector(exp.work.addAnother);
      if (!add) { report.push([`work[${i}]`, "SKIPPED no Add Another button"]); break; }
      add.click();
      await sleep(1200);
    }
    const scope = (sel) => nthWrapper(sel, i);

    for (const [domKey, profKey] of Object.entries(exp.fieldMap)) {
      if (!(domKey in exp.work)) continue;
      const wrapper = scope(exp.work[domKey]);
      if (!wrapper) continue;
      const val = job[profKey];

      if (domKey === "currentlyWorkHere") {
        const cb = wrapper.querySelector('input[type=checkbox]');
        // Tick BEFORE writing endDate: Workday deletes the End Date field outright when
        // this is checked, so the other order writes into a doomed node and the
        // "To is required" error never clears.
        if (cb && !!val && !cb.checked) { cb.click(); await sleep(900); }
        continue;
      }
      if (val == null || val === "" || val === "FILL_ME") continue;

      if (domKey === "startDate" || domKey === "endDate") {
        if (domKey === "endDate" && job.current) continue;   // field is gone by design
        const res = await setDate(ctx, cssPathOf(wrapper), val);
        report.push([`work[${i}].${domKey}`, res.ok ? "set" : `FAILED ${res.reason}`]);
      } else {
        const el = wrapper.querySelector("input,textarea");
        if (el) { setTextNative(el, String(val)); report.push([`work[${i}].${domKey}`, "set"]); }
      }
    }
  }

  // Education — degree is a short fixed listbox and profile.education.degree_level
  // matches its options verbatim ("Bachelors", NOT "Bachelor's Degree").
  const ed = exp.education;
  const school = dig(profile, "education.school");
  if (school) {
    const el = fieldInput(ed.school);
    if (el) { setTextNative(el, school); report.push(["education.school", "set"]); }
  }
  const degree = dig(profile, "education.degree_level");
  if (degree) {
    const res = await pickListbox(ctx, ed.degree, degree);
    report.push(["education.degree", res.ok ? `set ${res.value}` : `FAILED ${res.reason}`]);
  }
  const gpa = dig(profile, "education.gpa");
  if (gpa) {
    const el = fieldInput(ed.gpa);
    if (el) { setTextNative(el, String(gpa)); report.push(["education.gpa", "set"]); }
  }

  // Resume — required. Reuses fill.js's DataTransfer attach so there is one implementation.
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
    const label = extractLabel(w, adapter);
    if (!label) continue;

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
function nthWrapper(selector, i) {
  const all = [...document.querySelectorAll(selector)];
  return all[i] || all[0] || null;
}

// setDate takes a selector; give it a stable one for an element we already hold.
let _tmpId = 0;
function cssPathOf(el) {
  if (!el.id) el.id = `wd-drv-${++_tmpId}`;
  return `#${CSS.escape(el.id)}`;
}

// ── the loop ─────────────────────────────────────────────────────────────────

export async function runWorkday(ctx) {
  const { adapter, rpc } = ctx;
  const report = [];
  const needsInput = [];
  const steps = [];

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
      if (step.n >= adapter.multiStep.submitStep) {
        steps.push({ n: step.n, name: step.name, action: "stopped" });
        return { ok: true, adapter: "workday", stoppedAt: step, report, needsInput, steps,
                 note: "Reached Review. Submitting is deliberately left to you." };
      }

      const before = needsInput.length;
      if (step.n === 1) await fillMyInformation(ctx, report);
      else if (step.n === 2) await fillMyExperience(ctx, report);
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
