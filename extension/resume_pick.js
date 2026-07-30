// resume_pick.js — static best-of-4 resume selection for the Apply engine.
//
// Faithful JS port of profile.py `pick_resume`. Same scoring so the extension and
// the backend never disagree on which track to attach:
//   - each keyword found in the ROLE TITLE scores 2; in the JD BODY scores 1.
//   - tiebreak by "specificity" = total chars of matched phrases (title 2x, jd 1x),
//     so a longer/more-specific phrase match wins over several short ones.
//   - if nothing matches (best score <= 0), fall back to profile.default_resume_tag.
//
// `profile` is the bundled profile.json object (resume_tracks + default_resume_tag).
// Returns { tag, local_path, current_file, score } — mirrors the Python return.

export function pickResume(fieldText, jdText, profile) {
  const title = (fieldText || "").toLowerCase();
  const jd = (jdText || "").toLowerCase();

  let best = null;
  let bestKey = [-1, -1]; // [score, specificity]

  for (const track of profile.resume_tracks) {
    let score = 0;
    let spec = 0;
    for (const kRaw of track.keywords) {
      const k = kRaw.toLowerCase();
      if (title.includes(k)) { score += 2; spec += 2 * k.length; }
      if (jd.includes(k))    { score += 1; spec += k.length; }
    }
    if (score > bestKey[0] || (score === bestKey[0] && spec > bestKey[1])) {
      best = track;
      bestKey = [score, spec];
    }
  }

  let bestScore = bestKey[0];
  if (bestScore <= 0) { // nothing matched -> configured default track
    const tag = profile.default_resume_tag;
    best = profile.resume_tracks.find(t => t.tag === tag) || best;
    bestScore = 0;
  }

  return {
    tag: best.tag,
    local_path: best.local_path,
    current_file: best.current_file,
    score: bestScore,
  };
}

export default pickResume;
