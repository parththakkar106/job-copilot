#!/usr/bin/env python3
"""Tests for /edit, /resume and /myresume — changing an answer or the resume BEFORE the
✅, and sending the cached PDF to the phone.

Same harness as test_batch.py: point the modules at throwaway state, capture Telegram
sends and callback-answer toasts, and drive the handlers the way the poller does.
"""
import asyncio
import base64
import json
import tempfile
from pathlib import Path

import bridge as B
import pending as P
import triage as T

from server.store import use_temp_store
from server.store import get_store
from server.store.importer import import_profile

SENT: list = []
TOASTS: list = []
DOCS: list = []


def setup(tmp: Path):
    store = use_temp_store(tmp)
    # The resume tests read the profile's tracks, so bring the real profile in. Everything
    # else in this store stays empty and throwaway.
    import_profile(store)
    B.LOCK = asyncio.Lock()
    B.INBOX = asyncio.Queue()
    SENT.clear()
    TOASTS.clear()
    B.tg_send = lambda text, buttons=None: SENT.append((text, buttons)) or 1
    B.tg_answer_callback = lambda cb_id, text="": TOASTS.append(text)
    DOCS.clear()
    B.tg_send_document = lambda path, filename, caption="": (
        DOCS.append((str(path), filename, caption)) or 1)
    B.bank_draft = lambda item, company: (item.get("draft"), None)


def job(key, company="Ramp", title="Software Engineer"):
    return {"dedup_key": key, "company": company, "title": title,
            "location": "New York", "url": f"https://jobs.example/{key}",
            "compensation": "$100k-$150k", "resume": "Parth_Resume_tech_v4.pdf (tech track)"}


QS = [
    {"id": "ask_0", "label": "Why Ramp?", "required": True},
    {"id": "ask_1", "label": "Pronouns", "required": False,
     "options": ["He/Him", "She/Her", "They/Them"]},
]
FILLED = [{"label": "Email", "value": "jordan.lee@example.com"}]


def buttons_flat(bs):
    return [b["callback_data"] for row in (bs or []) for b in row]


async def test_edit_lists_answers():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    P.set_answer("aaa", "ask_0", "Because I love payments infra")
    P.set_answer("aaa", "ask_1", "They/Them")

    await B.handle_edit("/edit")
    text, _ = SENT[-1]
    assert "1. <b>Why Ramp?</b>: Because I love payments infra" in text, text
    n += 1
    assert "2. <b>Pronouns</b>: They/Them" in text, text
    n += 1
    return n


async def test_edit_one_shot():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    P.set_answer("aaa", "ask_0", "old answer")
    P.set_answer("aaa", "ask_1", "They/Them")

    await B.handle_edit("/edit 1 Because the team is elite")
    assert P.load("aaa")["questions"][0]["answer"] == "Because the team is elite"
    n += 1
    # complete + unapproved -> the summary is re-rendered so he can ✅
    assert any("ready to submit" in t for t, _ in SENT), [t for t, _ in SENT]
    n += 1
    return n


async def test_edit_reopen_re_asks():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    P.set_answer("aaa", "ask_0", "keep me")
    P.set_answer("aaa", "ask_1", "They/Them")

    await B.handle_edit("/edit 2")          # clear ask_1 and re-serve it
    assert P.load("aaa")["questions"][1]["answer"] is None, "re-open must clear the answer"
    n += 1
    text, _ = SENT[-1]
    assert "Pronouns" in text, text          # the question got served again
    n += 1
    assert P.load("aaa")["questions"][0]["answer"] == "keep me", "must not touch other answers"
    n += 1
    return n


async def test_edit_rejects_bad_number():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    await B.handle_edit("/edit 9")
    assert "1–2" in SENT[-1][0], SENT[-1][0]
    n += 1
    await B.handle_edit("/edit notanumber")
    assert "1–2" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


async def test_edit_no_questions_points_to_resume():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), [], FILLED)            # all-deterministic job (e.g. Chime)
    await B.handle_edit("/edit")
    assert "/resume" in SENT[-1][0] and "No free-text" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


async def test_edit_nothing_pending():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    await B.handle_edit("/edit")
    assert "Nothing in front of you" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


async def test_resume_shows_track_buttons():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    await B.handle_resume()
    text, bs = SENT[-1]
    cbs = buttons_flat(bs)
    tags = [t["tag"] for t in B.profile_mod.load_profile()["resume_tracks"]]
    for tag in tags:
        assert f"{B.CB}:r:aaa:{tag}" in cbs, (tag, cbs)
    n += 1
    assert "tech track" in text, text        # shows the current resume
    n += 1
    return n


async def test_resume_override_callback_sets_tag():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), [], FILLED)            # zero-question job so it's complete
    await B.batch_callback(f"{B.CB}:r:aaa:quant", "cb")
    rec = P.load("aaa")
    assert rec["resume_override"] == "quant", rec.get("resume_override")
    n += 1
    assert "quant track" in rec["resume"], rec["resume"]     # display string updated
    n += 1
    assert any("resume set to" in t for t, _ in SENT), [t for t, _ in SENT]
    n += 1
    return n


async def test_resume_override_unknown_track_noop():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), [], FILLED)
    await B.batch_callback(f"{B.CB}:r:aaa:bogus", "cb")
    assert P.load("aaa").get("resume_override") is None, "unknown track must not be stored"
    n += 1
    assert "Unknown track" in TOASTS[-1], TOASTS
    n += 1
    return n


async def test_resume_override_refused_after_approve():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), [], FILLED)
    P.approve("aaa")                         # he already ✅'d it
    await B.batch_callback(f"{B.CB}:r:aaa:quant", "cb")
    assert P.load("aaa").get("resume_override") is None, "must not change an approved job"
    n += 1
    assert "Already approved" in TOASTS[-1], TOASTS
    n += 1
    return n


async def test_resume_override_stale_button_no_crash():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    await B.batch_callback(f"{B.CB}:r:gone:quant", "cb")   # job already deleted
    assert "gone" in TOASTS[-1].lower(), TOASTS
    n += 1
    return n


def put_bytes(track: dict, cache: Path, raw: bytes) -> None:
    """Install `raw` as this track's PDF, the way core.resumes._write_cache does."""
    (cache / Path(track["local_path"]).name).write_bytes(raw)
    get_store().put_resume(track["tag"], raw, filename=Path(track["local_path"]).name)


def stub_cache(tmp: Path):
    """One PDF per track, in the store and in a cache directory B is pointed at.

    Both, because the sync writes both and the readers disagree about which they prefer:
    /myresume reads the stored bytes, and the extension build stages the files. A fixture
    that seeded only one of them would pass while the real command sent nothing.
    """
    cache = tmp / "resumes"
    cache.mkdir(parents=True, exist_ok=True)
    for track in B.profile_mod.load_profile()["resume_tracks"]:
        put_bytes(track, cache, b"%PDF-1.4 fake")
    # BOTH names. The bridge asks core.resumes where a track's PDF is, and that module
    # holds its own CACHE constant — pointing only paths.RESUMES at the fixture leaves the
    # lookup reading the real cache directory.
    B.paths_mod.RESUMES = cache
    B.resumes_mod.CACHE = cache
    B.resumes_mod.load_manifest = lambda: {"ai": {"synced": "2026-09-01"}}
    # No source folder in these tests: syncing is covered by test_resume_sync.py.
    B.sync_resumes = lambda why: []
    return cache


async def test_myresume_lists_tracks():
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    stub_cache(tmp)
    n = 0
    await B.handle_myresume("/myresume")
    text, buttons = SENT[-1]
    tags = [t["tag"] for t in B.profile_mod.load_profile()["resume_tracks"]]
    assert buttons_flat(buttons) == [f"{B.CB}:{B.CB_FILE}:{t}" for t in tags], buttons
    n += 1
    assert "Which resume?" in text, text
    n += 1
    # The sync date is the point of the list: a stale PDF is invisible once it's on the phone.
    assert "2026-09-01" in "".join(b["text"] for row in buttons for b in row), buttons
    n += 1
    assert not DOCS, "listing must not upload anything"
    n += 1
    return n


async def test_myresume_arg_sends_the_named_track():
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    cache = stub_cache(tmp)
    n = 0
    await B.handle_myresume("/myresume AI")     # case-insensitive
    path, filename, caption = DOCS[-1]
    assert path == str(cache / "ai.pdf"), path
    n += 1
    # Named the way an employer sees it, never the PDF's name on disk.
    track = next(t for t in B.profile_mod.load_profile()["resume_tracks"] if t["tag"] == "ai")
    assert filename == B.profile_mod.resume_filename(track), filename
    n += 1
    assert "ai" in caption, caption
    n += 1
    return n


async def test_myresume_button_sends_the_pdf():
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    cache = stub_cache(tmp)
    n = 0
    await B.batch_callback(f"{B.CB}:{B.CB_FILE}:quant", "cb")
    assert DOCS[-1][0] == str(cache / "quant.pdf"), DOCS
    n += 1
    assert TOASTS, "the button must be answered so its spinner stops"
    n += 1
    return n


async def test_myresume_unknown_track_says_so():
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    stub_cache(tmp)
    n = 0
    await B.handle_myresume("/myresume banana")
    assert not DOCS, "an unknown track must not send a file"
    n += 1
    assert "banana" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


async def test_myresume_track_without_a_path_is_not_the_cache_dir():
    """A track with no local_path must read as missing, not as the cache directory."""
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    stub_cache(tmp)
    n = 0
    real = B.profile_mod.load_profile
    B.profile_mod.load_profile = lambda: dict(
        real(), resume_tracks=[{"tag": "quant", "current_file": "x.pdf"}])
    try:
        await B.handle_myresume("/myresume quant")
    finally:
        B.profile_mod.load_profile = real
    assert not DOCS, "must not try to upload the cache directory"
    n += 1
    assert "sync-local" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


async def test_myresume_uncached_track_says_how_to_sync():
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    cache = stub_cache(tmp)
    (cache / "ds.pdf").unlink()
    n = 0
    await B.handle_myresume("/myresume ds")
    assert not DOCS, "a missing file must not be uploaded"
    n += 1
    assert "sync-local" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


class FakeWS:
    """Collects what a WS handler sends back, the way the extension would receive it."""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def answers(self):
        return self.sent[-1]["answers"]


async def test_resume_bytes_serves_the_synced_pdf():
    """The handler that removes the second stale copy: fresh bytes over the socket."""
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    cache = stub_cache(tmp)
    put_bytes({"tag": "ai", "local_path": "resumes/ai.pdf"}, cache,
              b"%PDF-1.4 the current one")
    n = 0
    synced = []
    B.sync_resumes = lambda why: synced.append(why) or []
    ws = FakeWS()
    await B.handle_resume_bytes({"reqId": "r1", "tag": "ai"}, ws)
    a = ws.answers()
    assert a["ok"], a
    n += 1
    # Synced BEFORE reading. Serving first and syncing after would hand out the old bytes
    # on every first request, which is the bug this whole path exists to remove.
    assert synced, "the handler must sync before it reads"
    n += 1
    assert base64.b64decode(a["b64"]) == b"%PDF-1.4 the current one", a["b64"][:40]
    n += 1
    assert a["sha256"] == B.resumes_mod.sha256(b"%PDF-1.4 the current one"), a["sha256"]
    n += 1
    track = next(t for t in B.profile_mod.load_profile()["resume_tracks"] if t["tag"] == "ai")
    assert a["file"] == B.profile_mod.resume_filename(track), a["file"]
    n += 1
    return n


async def test_resume_bytes_reports_failure_instead_of_raising():
    """The extension falls back to its bundled copy on ok:false, so a failure must be an
    answer, not an exception that leaves the socket waiting."""
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    stub_cache(tmp)
    B.sync_resumes = lambda why: []
    n = 0
    ws = FakeWS()
    await B.handle_resume_bytes({"reqId": "r1", "tag": "nosuch"}, ws)
    assert ws.answers()["ok"] is False, ws.answers()
    n += 1
    assert "nosuch" in ws.answers()["error"], ws.answers()
    n += 1

    ws = FakeWS()
    await B.handle_resume_bytes({"reqId": "r2"}, ws)       # no tag at all
    assert ws.answers()["ok"] is False, ws.answers()
    n += 1

    # A track that was never synced: read_cached raises SyncError, which must surface as
    # ok:false. Never-synced means no stored bytes, not merely an empty cache directory —
    # the store is the durable copy, and a hosted bridge has no cache directory at all.
    get_store().delete_resume("ai")
    B.paths_mod.RESUMES = tmp / "empty"
    B.resumes_mod.CACHE = tmp / "empty"
    ws = FakeWS()
    await B.handle_resume_bytes({"reqId": "r3", "tag": "ai"}, ws)
    assert ws.answers()["ok"] is False, ws.answers()
    n += 1
    return n


async def test_sync_resumes_never_raises():
    """An unconfigured source folder is a valid setup. It must not take the bridge down,
    and it must not lose the cache that is already there."""
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    n = 0
    real = B.resumes_mod.sync_local
    B.resumes_mod.sync_local = lambda: (_ for _ in ()).throw(
        B.resumes_mod.SyncError("no source folder"))
    try:
        assert B.sync_resumes("test") == [], "a SyncError must report nothing changed"
        n += 1
        B.resumes_mod.sync_local = lambda: (_ for _ in ()).throw(RuntimeError("disk on fire"))
        assert B.sync_resumes("test") == [], "an unexpected error must not propagate"
        n += 1
    finally:
        B.resumes_mod.sync_local = real
    return n


async def main():
    n = 0
    for t in (test_edit_lists_answers, test_edit_one_shot, test_edit_reopen_re_asks,
              test_edit_rejects_bad_number, test_edit_no_questions_points_to_resume,
              test_edit_nothing_pending, test_resume_shows_track_buttons,
              test_resume_override_callback_sets_tag, test_resume_override_unknown_track_noop,
              test_resume_override_refused_after_approve, test_resume_override_stale_button_no_crash,
              test_myresume_lists_tracks, test_myresume_arg_sends_the_named_track,
              test_myresume_button_sends_the_pdf, test_myresume_unknown_track_says_so,
              test_myresume_uncached_track_says_how_to_sync,
              test_myresume_track_without_a_path_is_not_the_cache_dir,
              test_resume_bytes_serves_the_synced_pdf,
              test_resume_bytes_reports_failure_instead_of_raising,
              test_sync_resumes_never_raises):
        n += await t()
    print(f"edit/resume: {n}/{n} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
