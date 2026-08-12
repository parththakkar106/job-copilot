// classify.js — JS port of profile.py's taxonomy resolver.
//
// The content script must bucket a field's label the SAME way the backend does, so
// this mirrors profile.py classify/dig/render/_buckets_for exactly. Kept behavior-
// identical on purpose; if taxonomy logic changes, change both (a parity test guards it).
//
// Buckets: deterministic -> Tier 0 silent fill (value_key -> profile.json)
//          draftable     -> Tier 2 answer_bank draft (answer_key)
//          always_ask    -> never auto-filled, surface to user
//          null          -> no rule matched, escalate to Tier 1

// Resolve a dotted value_key like 'identity.address.city' against profile.json.
// A numeric segment indexes a list ('work_history.0.employer') — kept in step with
// profile.py dig(), which the parity test enforces. Note `part in cur` would "work" on
// an array by accident (index in array is true), but negative indices and the bounds
// check would not, so the list case is handled explicitly in both ports.
export function dig(profile, dotted) {
  let cur = profile;
  for (const part of dotted.split(".")) {
    if (Array.isArray(cur)) {
      if (!/^-?\d+$/.test(part)) return null;
      let i = Number(part);
      if (i < 0) i += cur.length;
      if (i < 0 || i >= cur.length) return null;
      cur = cur[i];
      continue;
    }
    if (cur === null || typeof cur !== "object" || !(part in cur)) return null;
    cur = cur[part];
  }
  return cur;
}

const FILL_ME = "FILL_ME";

// Render a resolved value the way a text field expects it. Booleans -> Yes/No.
export function render(value) {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return value === null || value === undefined ? "" : String(value);
}

// Merge per-company override patterns (prepended) onto the base buckets.
function bucketsFor(tax, company) {
  const buckets = {};
  for (const [k, v] of Object.entries(tax.buckets)) buckets[k] = [...v];
  const overrides = tax.overrides || {};
  if (company && overrides[company]) {
    for (const [bucket, extra] of Object.entries(overrides[company])) {
      buckets[bucket] = [...extra, ...(buckets[bucket] || [])];
    }
  }
  return buckets;
}

// Ordered fallback answers for a deterministic rule, rendered ready to match. Mirrors
// profile.py resolve_alternates() — see the docstring there for the two sources and why
// a dotted entry that resolves beats treating it as a literal. Parity-tested.
export function resolveAlternates(profile, rule, valueKey) {
  const out = [];
  for (const entry of rule.alternates || []) {
    const val = typeof entry === "string" ? dig(profile, entry) : null;
    out.push(val !== null && val !== undefined ? render(val) : String(entry));
  }
  // The answer_alternates keys contain literal dots ("eeo.veteran_status" is ONE key),
  // so index the block whole rather than digging a compound path.
  const bank = dig(profile, "answer_alternates") || {};
  for (const a of bank[valueKey] || []) out.push(String(a));
  const seen = new Set();
  return out.filter(a => a && a !== FILL_ME && !seen.has(a) && seen.add(a));
}

// Core: given a field's visible text, return which bucket it falls in and — for
// deterministic — the resolved render-ready value.
// Returns { bucket, matched?, value_key?, value?, rendered?, filled?, alternates?, answer_key? }.
export function classify(fieldText, company, profile, tax) {
  const text = (fieldText || "").toLowerCase();
  const buckets = bucketsFor(tax, company);

  for (const bucket of tax.priority) {
    for (const rule of buckets[bucket] || []) {
      // Python uses re.search(..., IGNORECASE); 'i' flag matches. Patterns are simple
      // (no named groups/backrefs) so Python and JS regex agree. See parity test.
      let re;
      try { re = new RegExp(rule.pattern, "i"); }
      catch (e) { console.warn("[classify] bad pattern skipped:", rule.pattern, e); continue; }
      if (re.test(text)) {
        const out = { bucket, matched: rule.pattern };
        if (bucket === "deterministic") {
          const vk = rule.value_key;
          const val = dig(profile, vk);
          out.value_key = vk;
          out.value = val;
          out.rendered = render(val);
          out.filled = val !== null && val !== FILL_ME;
          out.alternates = resolveAlternates(profile, rule, vk);
        } else if (bucket === "draftable") {
          out.answer_key = rule.answer_key;
        }
        return out;
      }
    }
  }
  return { bucket: null };
}

export default classify;
