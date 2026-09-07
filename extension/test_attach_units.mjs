#!/usr/bin/env node
/**
 * Unit tests for attach.js — the outgoing filename and where the bytes come from.
 *
 * Two things here decide what an employer receives: the name on the file, and whether the
 * PDF is the current one. Both used to be wrong in ways nothing surfaced, so both are
 * asserted here rather than left to a live run.
 *
 * Run: node extension/test_attach_units.mjs
 */
import { resumeFilename, attachResume } from "./attach.js";

let n = 0;
const ok = (cond, msg) => { if (!cond) { console.error("FAIL:", msg); process.exit(1); } n++; };
const eq = (got, want, msg) => ok(got === want, `${msg} (got ${JSON.stringify(got)}, want ${JSON.stringify(want)})`);

const PROFILE = {
  identity: { first_name: "Ada", last_name: "Lovelace" },
  resume_tracks: [
    { tag: "quant", current_file: "Ada Quant Resume v4.pdf", local_path: "resumes/quant.pdf" },
    { tag: "ds", current_file: "ds_final.pdf", local_path: "resumes/ds.pdf" },
    { tag: "tech", current_file: "FILL_ME", local_path: "resumes/tech.pdf" },
    { tag: "ai", current_file: "Ada AI ML Resume.pdf", local_path: "resumes/ai.pdf" },
  ],
};
const track = (tag) => PROFILE.resume_tracks.find(t => t.tag === tag);

// ── the name ────────────────────────────────────────────────────────────────────────
eq(resumeFilename(track("ai"), PROFILE), "Ada_Lovelace_a.pdf",
   "the AI track's messy source name is ignored");
eq(resumeFilename(track("quant"), PROFILE), "Ada_Lovelace_q.pdf", "quant gets its initial");
eq(resumeFilename(track("tech"), PROFILE), "Ada_Lovelace_t.pdf",
   "an unfilled current_file is irrelevant once the identity is set");
eq(resumeFilename(track("quant"), { ...PROFILE, resume_tracks: [track("quant")] }),
   "Ada_Lovelace.pdf", "one track carries no suffix");
eq(resumeFilename(track("ds"), { identity: {}, resume_tracks: PROFILE.resume_tracks }),
   "ds_final.pdf", "no identity falls back to the source name");
eq(resumeFilename(track("tech"), { identity: {}, resume_tracks: PROFILE.resume_tracks }),
   "tech.pdf", "no identity and no source name never sends FILL_ME.pdf");
eq(resumeFilename(track("ai"), { identity: { full_name: "Ada Lovelace" },
                                 resume_tracks: PROFILE.resume_tracks }),
   "Ada_Lovelace_a.pdf", "full_name covers a profile with no split name");

// ── where the bytes come from ───────────────────────────────────────────────────────
// A DataTransfer/File stub: node has neither, and the real ones are the browser's job.
class FakeFile { constructor(parts, name) { this.parts = parts; this.name = name; } }
globalThis.File = FakeFile;
globalThis.atob = (b64) => Buffer.from(b64, "base64").toString("binary");
globalThis.DataTransfer = class { constructor() { this.files = []; this.items = { add: (f) => this.files.push(f) }; } };
globalThis.Event = class { constructor(type) { this.type = type; } };

function input() {
  return { files: { length: 0 }, dispatched: [],
           dispatchEvent(e) { this.dispatched.push(e.type); return true; } };
}
// attachFile assigns input.files from the DataTransfer, so length reads back as 1.
const PDF_B64 = Buffer.from("%PDF-1.4 fresh").toString("base64");

{
  const el = input();
  const rpc = async (msg) => {
    ok(msg.type === "RESUME_BYTES" && msg.tag === "ai", "asks the bridge for this track");
    return { ok: true, b64: PDF_B64, file: "Ada_Lovelace_a.pdf" };
  };
  const r = await attachResume(el, { ...track("ai"), send_as: "Ada_Lovelace_a.pdf" },
                               { rpc, profile: PROFILE });
  eq(r.source, "bridge", "bridge bytes are preferred over the bundled copy");
  eq(el.files[0].name, "Ada_Lovelace_a.pdf", "the bridge's name is used");
  ok(r.attached, "the input accepted the file");
}

{
  // Bridge down. The bundled copy is fetched instead, and the report says so — that flag
  // is the only way to tell a build-time PDF from a current one after the fact.
  const el = input();
  globalThis.chrome = { runtime: { getURL: (p) => `chrome-extension://x/${p}` } };
  globalThis.fetch = async () => ({ ok: true, blob: async () => "BUNDLED" });
  const rpc = async () => { throw new Error("bridge disconnected"); };
  const r = await attachResume(el, { ...track("ds"), send_as: "Ada_Lovelace_d.pdf" },
                               { rpc, profile: PROFILE });
  eq(r.source, "bundle", "a dead bridge falls back rather than failing the fill");
  eq(el.files[0].name, "Ada_Lovelace_d.pdf", "the fallback still uses the resolved name");
}

{
  // An answer the bridge could not fulfil (source folder gone, unknown track) is not a
  // reason to attach nothing.
  const el = input();
  const r = await attachResume(el, track("quant"), { rpc: async () => ({ ok: false, error: "x" }),
                                                     profile: PROFILE });
  eq(r.source, "bundle", "ok:false falls back to the bundle");
}

{
  // No rpc at all: the popup and any caller without the bridge still attach.
  const el = input();
  const r = await attachResume(el, track("quant"), { profile: PROFILE });
  eq(r.source, "bundle", "no rpc means the bundled copy, not a crash");
}

console.log(`attach units: ${n}/${n} checks passed`);
