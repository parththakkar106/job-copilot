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

// Decode a base64 PDF into a File. The bridge sends base64 because the socket carries
// JSON, and a PDF is not text.
function fileFromBase64(b64, filename) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new File([bytes], filename, { type: "application/pdf" });
}

// Ask the bridge for this track's bytes. Null on any failure, which is the signal to fall
// back to the bundled copy.
//
// THE BUNDLED COPY IS A BUILD ARTEFACT. extension/resumes/*.pdf is written by build.py, so
// it is stale from the first edit after that build, and nothing in the browser can detect
// that. The bridge re-syncs from the source folder before answering, so bytes that arrive
// here are current as of this attach.
// A reachable but unresponsive bridge must not stall the fill. The worker's own ask
// timeout is 30 minutes, which is right for a question waiting on a human and far too long
// for a local file read, so cap this one separately and fall back when it expires.
const BRIDGE_RESUME_TIMEOUT_MS = 15000;

async function bridgeResume(track, rpc) {
  if (!rpc || !track.tag) return null;
  try {
    const r = await Promise.race([
      rpc({ type: "RESUME_BYTES", tag: track.tag }),
      new Promise(res => setTimeout(() => res(null), BRIDGE_RESUME_TIMEOUT_MS)),
    ]);
    if (!r || !r.ok || !r.b64) return null;
    return fileFromBase64(r.b64, r.file || track.send_as || `${track.tag}.pdf`);
  } catch (_) {
    return null;                  // bridge down, worker evicted, timeout: use the bundle
  }
}

// Load by track then attach. `deps.rpc` reaches the bridge for fresh bytes; `deps.profile`
// covers callers that have not resolved `track.send_as` themselves.
//
// Returns { attached, source } — `source` is "bridge" or "bundle", so the fill report can
// say when an application used the build-time copy and may therefore be behind.
export async function attachResume(inputEl, track, deps = {}) {
  const { rpc, profile } = deps;
  const name = track.send_as || resumeFilename(track, profile);
  let file = await bridgeResume(track, rpc);
  const source = file ? "bridge" : "bundle";
  if (!file) file = await loadResumeFile(track.local_path, name);
  return { attached: attachFile(inputEl, file), source };
}

export default attachResume;
