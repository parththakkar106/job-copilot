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
export function dig(profile, dotted) {
  let cur = profile;
  for (const part of dotted.split(".")) {
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

// Core: given a field's visible text, return which bucket it falls in and — for
// deterministic — the resolved render-ready value.
// Returns { bucket, matched?, value_key?, value?, rendered?, filled?, answer_key? }.
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
