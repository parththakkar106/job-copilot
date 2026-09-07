// attach.js — inject a resume PDF into a file <input> via DataTransfer.
//
// The extension owns the resume bytes (staged into extension/resumes/ by build.py),
// so uploading is scriptable: build a File, stuff it into a DataTransfer, assign it to
// input.files, then dispatch change/input so the ATS uploader reacts as if the user
// picked the file. Works on Greenhouse/Lever/Ashby native file inputs.

// Fetch a staged resume's bytes from inside the extension bundle.
// `localPath` is e.g. "resumes/quant.pdf" (from profile.json resume_tracks).
export async function loadResumeFile(localPath, filename) {
  const url = chrome.runtime.getURL(localPath);
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`resume fetch failed (${resp.status}) for ${localPath}`);
  const blob = await resp.blob();
  return new File([blob], filename, { type: "application/pdf" });
}

// Attach `file` to a file input element. Returns true on success.
export function attachFile(inputEl, file) {
  if (!inputEl) return false;
  const dt = new DataTransfer();
  dt.items.add(file);
  inputEl.files = dt.files;
  // CHECK BEFORE DISPATCHING. The question this answers is "did the input accept the
  // file", and after the events it is no longer being asked: Workday's change handler runs
  // synchronously, consumes the file and resets input.files to empty, so reading the
  // length afterwards returned 0 and reported a rejection for an upload that had in fact
  // succeeded — the container said "successfully uploaded" in the very same breath.
  const accepted = inputEl.files.length === 1;
  // Some uploaders listen on input, others on change — fire both, bubbling.
  inputEl.dispatchEvent(new Event("input", { bubbles: true }));
  inputEl.dispatchEvent(new Event("change", { bubbles: true }));
  return accepted;
}

// The visible filename an employer sees: built from identity, never from the PDF's own
// name. Port of profile.py `resume_filename` — keep the two in step.
//
// The source filename is whatever the file is called on disk, and after `sync-local` that
// is the Finals-folder name, so deriving from it sent a different string per track. The
// suffix is the tag's FIRST LETTER, and only when the profile defines more than one
// track: the Workday adapter reads the attached track back off the filename
// (`attachedNamePattern`), which needs those letters to stay distinct.
export function resumeFilename(track, profile) {
  const ident = (profile && profile.identity) || {};
  const clean = (v) => (v && v !== "FILL_ME" ? String(v) : "")
    .replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

  let stem = [clean(ident.first_name), clean(ident.last_name)].filter(Boolean).join("_");
  if (!stem) stem = clean(ident.full_name);
  // No identity to build from: an oddly named CV beats one called FILL_ME.pdf, and an
  // unfilled track carries that placeholder rather than omitting the key.
  if (!stem) {
    const src = track.current_file;
    return src && src !== "FILL_ME" ? src : `${track.tag || "resume"}.pdf`;
  }

  const tracks = (profile && profile.resume_tracks) || [];
  const tag = String(track.tag || "");
  if (tracks.length > 1 && tag) stem += `_${tag[0].toLowerCase()}`;
  const src = String(track.local_path || track.current_file || "x.pdf");
  const ext = src.includes(".") ? src.slice(src.lastIndexOf(".")) : ".pdf";
  return `${stem}${ext}`;
}

// Convenience: load by track then attach. `track.send_as` is the name fill.js already
// resolved for this run; `profile` covers the callers that have not resolved one.
export async function attachResume(inputEl, track, profile) {
  const name = track.send_as || resumeFilename(track, profile);
  const file = await loadResumeFile(track.local_path, name);
  return attachFile(inputEl, file);
}

export default attachResume;
