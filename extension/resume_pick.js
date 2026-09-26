// resume_pick.js — static resume track selection for the Apply engine.
//
// Faithful JS port of profile.py `pick_resume`. Same rules so the extension and the
// backend never disagree on which track to attach:
//   1. `field`, the scraper's classification of the role (queue.json `field`), wins when
//      it names a track. filters.yaml curates that list, so it is the strongest signal.
//   2. Otherwise the FIRST track, in profile order, with a keyword in the ROLE TITLE wins.
//      Profile order is the priority: list specialist tracks (quant, ai, ds) before the
//      generic one (tech). Scoring by match count used to send "AI Engineer" to tech,
//      because "engineer" matched tech as often as "ai" matched ai and was longer.
//   3. Otherwise the track with the most keywords in the JD BODY wins.
//   4. If nothing matches, fall back to profile.default_resume_tag.
// Keywords match as whole words, with an optional plural "s", so "ai" never matches
// inside "email" or "AIaaS".
//
// `profile` is the bundled profile.json object (resume_tracks + default_resume_tag).
// Returns { tag, local_path, current_file, score } — mirrors the Python return.

function hasWord(text, kw) {
  const k = kw.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`\\b${k}s?\\b`).test(text);
}

export function pickResume(fieldText, jdText, profile, field) {
  const title = (fieldText || "").toLowerCase();
  const jd = (jdText || "").toLowerCase();
  const tracks = profile.resume_tracks;

  let best = tracks.find(t => field && t.tag === field) || null;
  let score = best ? 3 : 0;
  if (!best) {
    best = tracks.find(t => t.keywords.some(k => hasWord(title, k))) || null;
    if (best) score = 2;
  }
  if (!best && jd) {
    let most = 0;
    for (const t of tracks) {
      const n = t.keywords.filter(k => hasWord(jd, k)).length;
      if (n > most) { best = t; most = n; }
    }
    if (best) score = 1;
  }
  if (!best) { // nothing matched -> configured default track
    best = tracks.find(t => t.tag === profile.default_resume_tag) || tracks[0];
    score = 0;
  }

  return {
    tag: best.tag,
    local_path: best.local_path,
    current_file: best.current_file,
    score,
  };
}

export default pickResume;
