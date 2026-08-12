#!/usr/bin/env python3
"""Parity: core/profile.py resolve_stream/dig  ==  extension/{stream,classify}.js.

The backend and the extension both collapse the profile to one resume track's wording. If
they ever disagree, the PDF the extension attaches and the text it types into the form come
from different streams — the exact mismatch resolve_stream exists to prevent, reintroduced
silently. So run BOTH implementations over the same fixture and compare the JSON.

Run:  python test_stream_parity.py        (skips cleanly if node is unavailable)
"""
from __future__ import annotations
import json, shutil, subprocess, sys, tempfile, pathlib

from core import profile as P

HERE = pathlib.Path(__file__).parent
EXT = HERE / "extension"

# Deliberately awkward fixture: an override that only partially shadows its default, a
# stream with no entry at all, nested dicts and lists inside an override, a null, and a
# key that exists ONLY in the override.
FIXTURE = {
    "identity": {"first_name": "A", "nested": {"deep": {"keep": 1, "swap": "base"}}},
    "work_history": [
        {
            "employer": "Co",                    # fact: must survive every stream
            "title": "Analyst",
            "end": None,                         # null must stay null, not become ""
            "current": True,
            "description": "base desc",
            "by_stream": {
                "quant": {"description": "quant desc"},
                "ai": {"description": "ai desc", "extra_only_in_override": ["x", {"y": 2}]},
                # note: no "ds" and no "tech" -> both must fall through to base
            },
        },
        {"employer": "Co2", "description": "second base", "by_stream": {"ds": {"description": "ds two"}}},
    ],
    "by_stream": {"quant": {"identity": {"first_name": "QUANT-A"}}},  # top-level override
    "resume_tracks": [{"tag": t} for t in ("quant", "ds", "tech", "ai")],
}

TAGS = ["quant", "ds", "tech", "ai", "unknown-tag"]

DIG_KEYS = [
    "work_history.0.employer",
    "work_history.0.description",
    "work_history.1.description",
    "work_history.0.end",
    "work_history.5.employer",        # out of range -> None
    "work_history.-1.employer",       # negative index
    "work_history.notanindex.x",      # non-numeric on a list -> None
    "identity.nested.deep.keep",
    "identity.missing.key",
    "work_history.0.by_stream",       # must be GONE after resolution
]

# resolve_alternates decides the ORDER answers are offered to a control, so a drift
# between the two ports means the backend and the extension would fill different values
# into the same field — the same class of silent mismatch resolve_stream guards against.
# Cases cover both entry kinds (a dotted path that resolves vs a literal), the merge with
# answer_alternates, de-duplication, and the FILL_ME/blank drops.
# Deliberately NON-personal key names: this checks merge order and de-duplication, which
# have nothing to do with what the values mean, and a fixture spelling out real EEO
# answers is exactly what pii_guard exists to keep out of this repo.
ALT_PROFILE = {
    "colour": {"primary": "Blue", "secondary": "Green"},
    "flags": {"agreed": True, "unset": "FILL_ME"},
    "answer_alternates": {
        "colour.primary": ["Azure", "Blue", ""],    # dupe + blank, both dropped
        "shape.side": ["Round"],
    },
}
ALT_CASES = [
    # (rule, value_key)
    ({"alternates": ["colour.secondary"]}, "colour.primary"),    # path resolves -> its value
    ({"alternates": ["flags.agreed"]}, "shape.side"),            # boolean -> "Yes"
    ({"alternates": ["I've already graduated"]}, "when.done"),   # literal, not a path
    ({"alternates": ["flags.unset"]}, "colour.primary"),         # FILL_ME dropped
    ({}, "colour.primary"),                                      # bank only
    ({}, "nothing.here"),                                        # neither source -> []
]

JS = r"""
import { resolveStream } from "./stream.js";
import { dig, resolveAlternates } from "./classify.js";
const [fixture, tags, keys, altProfile, altCases] = JSON.parse(process.argv[2]);
const out = {};
for (const tag of tags) {
  const resolved = resolveStream(fixture, tag);
  out[tag] = { resolved, dug: Object.fromEntries(keys.map(k => [k, dig(resolved, k)])) };
}
out._alternates = altCases.map(([rule, vk]) => resolveAlternates(altProfile, rule, vk));
process.stdout.write(JSON.stringify(out));
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node not on PATH — cannot check JS parity")
        return 0

    py = {}
    for tag in TAGS:
        resolved = P.resolve_stream(FIXTURE, tag)
        py[tag] = {"resolved": resolved,
                   "dug": {k: P.dig(resolved, k) for k in DIG_KEYS}}

    with tempfile.TemporaryDirectory() as td:
        runner = pathlib.Path(td) / "run_parity.mjs"
        runner.write_text(JS, encoding="utf-8")
        # Import specifiers in JS are relative to the runner, so run it from extension/.
        shutil.copy(runner, EXT / "_parity_tmp.mjs")
        try:
            r = subprocess.run(
                [node, "_parity_tmp.mjs",
                 json.dumps([FIXTURE, TAGS, DIG_KEYS, ALT_PROFILE, ALT_CASES])],
                cwd=EXT, capture_output=True, text=True)
        finally:
            (EXT / "_parity_tmp.mjs").unlink(missing_ok=True)

    if r.returncode != 0:
        print("FAIL: node runner errored\n" + (r.stderr or "")[:2000])
        return 1

    js = json.loads(r.stdout)
    fails = []
    for tag in TAGS:
        if js[tag]["resolved"] != py[tag]["resolved"]:
            fails.append(f"  [{tag}] resolved profiles differ\n"
                         f"      py: {json.dumps(py[tag]['resolved'], sort_keys=True)[:300]}\n"
                         f"      js: {json.dumps(js[tag]['resolved'], sort_keys=True)[:300]}")
        for k in DIG_KEYS:
            p, j = py[tag]["dug"][k], js[tag]["dug"][k]
            if p != j:
                fails.append(f"  [{tag}] dig({k!r}): py={p!r} js={j!r}")

    for (rule, vk), j in zip(ALT_CASES, js["_alternates"]):
        p = P.resolve_alternates(ALT_PROFILE, rule, vk)
        if p != j:
            fails.append(f"  resolve_alternates({rule}, {vk!r}): py={p!r} js={j!r}")

    # Content assertions — parity alone would pass if BOTH sides were wrong together.
    q = py["quant"]["resolved"]["work_history"][0]
    checks = [
        ("by_stream consumed", "by_stream" not in q),
        ("override applied", q["description"] == "quant desc"),
        ("facts survive override", q["employer"] == "Co" and q["title"] == "Analyst"),
        ("null preserved", q["end"] is None),
        ("missing stream falls back",
         py["ds"]["resolved"]["work_history"][0]["description"] == "base desc"),
        ("unknown tag falls back",
         py["unknown-tag"]["resolved"]["work_history"][0]["description"] == "base desc"),
        ("per-entry override independent",
         py["ds"]["resolved"]["work_history"][1]["description"] == "ds two"),
        ("override-only key appears",
         "extra_only_in_override" in py["ai"]["resolved"]["work_history"][0]),
        ("top-level override applied",
         py["quant"]["resolved"]["identity"]["first_name"] == "QUANT-A"),
        ("top-level override does not clobber siblings",
         py["quant"]["resolved"]["identity"]["nested"]["deep"]["keep"] == 1),
        ("list index digs", P.dig(q, "description") == "quant desc"),
        ("out-of-range dig is None", py["quant"]["dug"]["work_history.5.employer"] is None),
        ("non-numeric list segment is None", py["quant"]["dug"]["work_history.notanindex.x"] is None),
    ]
    for name, ok in checks:
        if not ok:
            fails.append(f"  behaviour: {name}")

    if fails:
        print(f"FAIL ({len(fails)}):")
        print("\n".join(fails))
        return 1
    print(f"OK: {len(TAGS)} tags x {len(DIG_KEYS)} keys in parity, "
          f"{len(ALT_CASES)} alternates cases in parity, "
          f"{len(checks)} behaviour checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
