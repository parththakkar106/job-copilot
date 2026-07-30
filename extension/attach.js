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
  // Some uploaders listen on input, others on change — fire both, bubbling.
  inputEl.dispatchEvent(new Event("input", { bubbles: true }));
  inputEl.dispatchEvent(new Event("change", { bubbles: true }));
  return inputEl.files.length === 1;
}

// The visible filename an employer sees: drop the internal version suffix and collapse the
// track word to a single letter — Parth_Resume_tech_v4.pdf -> Parth_Resume_t.pdf
// (tech->t, ai->a, quant->q, ds->d).
export function cleanResumeName(filename, tag) {
  let name = String(filename).replace(/_v\d+/i, ""); // strip version suffix
  if (tag) name = name.replace(new RegExp(`_${tag}(?=\\.[^.]+$)`, "i"), `_${tag[0].toLowerCase()}`);
  return name;
}

// Convenience: load by track then attach.
export async function attachResume(inputEl, track) {
  const file = await loadResumeFile(track.local_path, cleanResumeName(track.current_file, track.tag));
  return attachFile(inputEl, file);
}

export default attachResume;
