#!/usr/bin/env node
/**
 * Unit tests for the extension half of the AI fill + verify pass.
 *
 *   runAiPass         the bridge call. Every reason not to run one has to look the same to
 *                     the caller (an empty decision list), because they all mean the same
 *                     thing: keep the deterministic fill.
 *   applyAiDecisions  what actually lands on the form, and what Parth is then shown. Each
 *                     branch here is a distinct way to mislead him — a flag that writes, a
 *                     proposal that writes, a report row that vouches for a value the
 *                     setter refused, or a question still asked after it was answered.
 *
 * Same trick as test_fill_units.mjs: fill.js is a content script with no exports, so the
 * real function source is pulled out and evaluated. A copy-pasted duplicate would only
 * ever test the copy.
 *
 * Run: node extension/test_aipass_units.mjs
 */
import { readFileSync } from "fs";

const src = readFileSync(new URL("./fill.js", import.meta.url), "utf8");

function extract(name) {
  const i = src.search(new RegExp(`(async )?function ${name}\\(`));
  if (i < 0) throw new Error(`${name}() not found in fill.js — did it get renamed?`);
  let depth = 0, j = src.indexOf("{", i);
  for (; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) break;
  }
  return src.slice(i, j + 1);
}

function extractConst(name) {
  const i = src.indexOf(`const ${name} = `);
  if (i < 0) throw new Error(`const ${name} not found in fill.js — did it get renamed?`);
  for (let j = src.indexOf("\n", i); j > 0; j = src.indexOf("\n", j + 1)) {
    if (src.slice(i, j).trimEnd().endsWith(";")) return src.slice(i, j);
  }
  throw new Error(`const ${name} has no terminating semicolon`);
}

function extractConsts() {
  const start = src.indexOf("const norm = ");
  const wbAt = src.indexOf("const wb = ", start);
  const end = src.indexOf("\n", wbAt);
  return src.slice(start, end);
}

// runAiPass talks to the bridge through the module-level `rpc`, so it is injected rather
// than stubbed globally — that keeps each case's fake local to the case.
const { applyAiDecisions, makeRunAiPass } = new Function(
  `${extractConsts()}\n${extract("readableLabel")}\n${extractConst("fieldKey")}\n` +
  `${extract("applyAiDecisions")}\n` +
  `const makeRunAiPass = (rpc) => { ${extract("runAiPass")} return runAiPass; };\n` +
  `return { applyAiDecisions, makeRunAiPass };`)();

let n = 0;
const ok = (cond, msg) => { if (!cond) { console.error("FAIL:", msg); process.exit(1); } n++; };

// --- runAiPass ---------------------------------------------------------------------

const poolOf = (...labels) => labels.map((label, i) => ({
  id: `q${i + 1}`, label, apply: async () => ({ ok: true }),
  meta: { id: `q${i + 1}`, label },
}));

{
  // Nothing on the form: no call at all. The pass costs a Claude call, and a call that
  // can only answer about nothing is the easiest one to not make.
  let called = 0;
  const run = makeRunAiPass(async () => { called++; return { enabled: true, decisions: [{ id: "x" }] }; });
  ok((await run([], {})).length === 0, "empty pool returns no decisions");
  ok(called === 0, "empty pool does not call the bridge");
}

{
  // The toggle lives in the bridge. OFF has to look exactly like every other reason not
  // to run, or the extension starts carrying a copy of a preference it cannot see.
  const run = makeRunAiPass(async () => ({ enabled: false, decisions: [{ id: "q1" }] }));
  ok((await run(poolOf("Email"), {})).length === 0, "disabled means no decisions, whatever came back");
}

{
  const seen = [];
  const run = makeRunAiPass(async (msg) => { seen.push(msg); return { enabled: true, decisions: [{ id: "q1", action: "fill" }] }; });
  const out = await run(poolOf("Email", "Phone"), { company: "Ramp" });
  ok(out.length === 1 && out[0].id === "q1", "decisions come back through");
  ok(seen.length === 1, "one call for the whole form, not one per field");
  ok(seen[0].type === "AI_PASS", "sends AI_PASS");
  ok(seen[0].fields.length === 2, "sends every pooled field");
  // The apply closures stay on this side — they are not serializable and the bridge has
  // no use for them.
  ok(seen[0].fields.every(f => !f.apply), "sends the metadata only, never the setters");
  ok(seen[0].context.company === "Ramp", "sends the job context");
}

{
  // A bridge that is down, or a reply that makes no sense, must not take the fill with it.
  const dead = makeRunAiPass(async () => { throw new Error("bridge disconnected"); });
  ok((await dead(poolOf("Email"), {})).length === 0, "a dead bridge means no decisions, not an error");
  const junk = makeRunAiPass(async () => ({ enabled: true, decisions: "not an array" }));
  ok((await junk(poolOf("Email"), {})).length === 0, "a malformed reply is ignored");
  const empty = makeRunAiPass(async () => null);
  ok((await empty(poolOf("Email"), {})).length === 0, "no reply at all is ignored");
}

// --- applyAiDecisions --------------------------------------------------------------

function harness(entries) {
  const writes = [];
  const pool = entries.map(e => ({
    id: e.id, label: e.label,
    apply: async (v) => { writes.push({ id: e.id, value: v }); return e.result || { ok: true }; },
  }));
  const report = { filled: [], needsInput: [], flagged: [], optional: [], skipped: [], errors: [] };
  return { pool, report, writes };
}

{
  // A fill lands on the form, is reported as the AI's work, and stops being a question.
  const h = harness([{ id: "q1", label: "Anything else?" }]);
  h.report.needsInput.push({ label: "Anything else?", _askId: "q1", why: "unrecognized required field" });
  const asks = [{ id: "q1", label: "Anything else?" }];
  const d = { id: "q1", action: "fill", value: "A few things.", confidence: 0.9 };
  const wrote = await applyAiDecisions([d], h.pool, h.report, asks);

  ok(h.writes.length === 1 && h.writes[0].value === "A few things.", "a fill is written through the field's own setter");
  ok(d.applied === true, "the decision records that it landed");
  ok(wrote.length === 1, "the trace names what was written");
  ok(asks.length === 0, "an answered question stops being asked");
  ok(h.report.needsInput.length === 0, "and drops out of the needsInput pile");
  ok(h.report.filled.length === 1, "one report row");
  ok(/^AI fill/.test(h.report.filled[0].via), "the row says the AI wrote it");
}

{
  // A replacement REPLACES its deterministic row. Two rows for one control is how a
  // review ends up vouching for a value that is no longer on the form.
  const h = harness([{ id: "q1", label: "Projects" }]);
  h.report.filled.push({ label: "Projects", value: "https://github.com/x" });
  const d = { id: "q1", action: "replace", value: "A few, including github.com/x", confidence: 0.9, was: "https://github.com/x" };
  await applyAiDecisions([d], h.pool, h.report, []);
  ok(h.report.filled.length === 1, "the deterministic row is replaced, not duplicated");
  ok(h.report.filled[0].value === "A few, including github.com/x", "the row shows what is on the form now");
  ok(d.was === "https://github.com/x", "the old value is kept for the review");
}

{
  // The two actions that must never touch the form.
  const h = harness([{ id: "q1", label: "How did you hear about us?" },
                     { id: "q2", label: "Desired salary" }]);
  const asks = [{ id: "q1", label: "How did you hear about us?" },
                { id: "q2", label: "Desired salary", required: true }];
  const flag = { id: "q1", action: "flag", value: "LinkedIn", was: "Website", confidence: 0.4 };
  const propose = { id: "q2", action: "propose", value: "150000", confidence: 0.99 };
  await applyAiDecisions([flag, propose], h.pool, h.report, asks);

  ok(h.writes.length === 0, "neither a flag nor a proposal writes anything");
  ok(flag.applied === false && propose.applied === false, "and neither claims it did");
  ok(h.report.filled.length === 0, "nothing is reported as filled");
  ok(asks.length === 2, "both questions are still asked");
  // A proposal becomes the draft on the question Parth is already being asked, so one tap
  // accepts it. A flag does not — there is nothing to accept, the field already has a value.
  ok(asks[1].draft === "150000", "an always_ask proposal is offered as a draft");
  ok(asks[0].draft === undefined, "a flag is not offered as a draft");
}

{
  // An existing draft (his own bank answer) is not overwritten by a proposal.
  const h = harness([{ id: "q1", label: "Desired salary" }]);
  const asks = [{ id: "q1", label: "Desired salary", draft: "My own saved number" }];
  await applyAiDecisions([{ id: "q1", action: "propose", value: "150000" }], h.pool, h.report, asks);
  ok(asks[0].draft === "My own saved number", "a saved answer beats a proposal");
}

{
  // The setter refused. Nothing is on the form, so nothing may be reported as filled —
  // this is the exact shape of a review that vouches for an empty field.
  const h = harness([{ id: "q1", label: "Country", result: { ok: false, why: "no option matched" } }]);
  const asks = [{ id: "q1", label: "Country" }];
  const d = { id: "q1", action: "fill", value: "Freedonia" };
  const wrote = await applyAiDecisions([d], h.pool, h.report, asks);
  ok(d.applied === false, "a refused write is recorded as un-applied");
  ok(d.why_not === "no option matched", "with the setter's own reason");
  ok(h.report.filled.length === 0, "and is never reported as filled");
  ok(asks.length === 1, "so the question is still asked");
  ok(wrote.length === 0, "and the trace does not claim it");
}

{
  // Phase 3 replays phase 1's decisions onto a form that can genuinely come out
  // different. A decision with nowhere to land is dropped, never re-homed by label onto
  // whatever looks similar.
  const h = harness([{ id: "q1", label: "Email" }]);
  const d = { id: "gone", action: "fill", value: "x" };
  await applyAiDecisions([d], h.pool, h.report, []);
  ok(d.applied === false && /not on this page/.test(d.why_not), "a decision for a missing field is dropped");
  ok(h.writes.length === 0, "and writes nothing");
}

{
  // What the setter actually committed wins over what we asked for — a school that is not
  // in the ATS's list commits as "Other", and the report has to say "Other".
  const h = harness([{ id: "q1", label: "School", result: { ok: true, value: "Other" } }]);
  await applyAiDecisions([{ id: "q1", action: "fill", value: "Some University" }], h.pool, h.report, []);
  ok(h.report.filled[0].value === "Other", "the report records what the control holds");
}

{
  // Low confidence is written (a complete form he corrects beats a blocked one) but it
  // has to be visible as such, or it reads exactly like a value the rules were sure of.
  const h = harness([{ id: "q1", label: "Start date" }]);
  await applyAiDecisions([{ id: "q1", action: "fill", value: "Immediately", low: true }], h.pool, h.report, []);
  ok(/low confidence/.test(h.report.filled[0].via), "a low-confidence fill is marked in the report");
}

console.log(`ai-pass fill units: ${n}/${n} checks passed`);
