// stream.js — resolve the profile down to one resume track's wording.
//
// Faithful JS port of core/profile.py `resolveStream`/`resolve_stream`. Same collapse
// rules so the extension and the backend never disagree about what the form should say.
//
// WHY THIS EXISTS: the four resumes (quant/ds/tech/ai) describe the SAME two jobs in four
// different vocabularies — the quant PDF sells a MILP solver, the AI one sells a ReAct
// agent, and they are the same work. A form filled from a stream-blind profile contradicts
// the PDF stapled to it, which is the kind of mismatch a recruiter reads as embellishment.
//
// THE ORDER THAT MATTERS: pick the track FIRST (pickResume), resolve the profile through
// that tag, and only then start filling. Everything downstream — classify, dig, the
// adapters — then sees a plain profile of plain strings and needs no idea streams exist.
//
// `by_stream.<tag>` holds only the keys that differ; anything absent falls through to the
// sibling default. Employer, title, dates and location are FACTS and must never be varied
// by stream — only emphasis may.

export function resolveStream(node, tag) {
  if (Array.isArray(node)) return node.map(v => resolveStream(v, tag));
  if (node === null || typeof node !== "object") return node;

  const overrides = node.by_stream || {};
  const chosen = overrides[tag] || {};
  const out = {};
  for (const [k, v] of Object.entries(node)) {
    if (k === "by_stream") continue;          // consumed, never leaks into the result
    out[k] = resolveStream(v, tag);
  }
  for (const [k, v] of Object.entries(chosen)) {
    out[k] = merge(out[k], resolveStream(v, tag));
  }
  return out;
}

// Overlay `override` on `base`, recursing into dicts.
//
// A dict override MERGES rather than replaces. Overriding one key of a block must not
// delete the rest of it — `by_stream.quant.identity.first_name` shadows exactly that name
// and leaves the address, links and everything else standing. Plain replacement looks
// correct on a one-key fixture and quietly empties the block in production.
//
// Lists replace wholesale: there is no honest way to merge two ordered sequences (index
// alignment is a guess), so a stream that wants a different list writes the whole list.
function merge(base, override) {
  const isPlain = v => v !== null && typeof v === "object" && !Array.isArray(v);
  if (isPlain(base) && isPlain(override)) {
    const out = { ...base };
    for (const [k, v] of Object.entries(override)) out[k] = merge(out[k], v);
    return out;
  }
  return override;
}

// Convenience: the one call a fill should make at the top of an application.
// Returns the chosen track alongside the resolved profile so the caller attaches the PDF
// and fills the form from the same decision rather than making it twice.
export function resolveForRole(profile, pickResume, fieldText, jdText) {
  const track = pickResume(fieldText, jdText, profile);
  return { track, profile: resolveStream(profile, track.tag) };
}

export default resolveStream;
