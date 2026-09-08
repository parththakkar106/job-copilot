"""CLI for the store: `python -m server.store <command>`.

    import              read JOBCOPILOT_HOME into the database
    export [DIR]        write the database back out as files
    export --companies  print one config file to stdout, for a text-editor edit
    migrate             bring the schema to head
    status              row counts, so you can see what an import actually took
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import paths  # noqa: E402

from . import exporter, importer, migrate  # noqa: E402
from .engine import default_url  # noqa: E402
from .store import Store  # noqa: E402

#: The single-file exports, so `--companies` can print one without writing the rest.
SINGLE = {
    "profile": (exporter.export_profile, "profile.json"),
    "companies": (exporter.export_companies, "companies.yaml"),
    "answers": (exporter.export_answers, "answer_bank.yaml"),
    "applied": (exporter.export_applied, "applied.json"),
    "outreach": (exporter.export_outreach, "outreach.json"),
    "prefs": (exporter.export_prefs, "prefs.json"),
}


def _store(args) -> Store:
    return Store.from_url(args.url or default_url())


def cmd_import(args) -> int:
    store = _store(args)
    counts = importer.import_all(store)
    for k, v in counts.items():
        print(f"{k:10} {v}")
    return 0


def cmd_export(args) -> int:
    store = _store(args)
    for name, (fn, _) in SINGLE.items():
        if getattr(args, name, False):
            sys.stdout.write(fn(store, None))
            return 0
    if not args.directory:
        print("export needs a directory, or one of "
              f"{', '.join('--' + n for n in SINGLE)}", file=sys.stderr)
        return 2
    for k, v in exporter.export_all(store, Path(args.directory)).items():
        print(f"{k:10} {v}")
    return 0


def cmd_migrate(args) -> int:
    print(migrate.upgrade(args.url))
    return 0


def cmd_status(args) -> int:
    store = _store(args)
    print(f"database   {args.url or default_url()}")
    print(f"home       {paths.STATE}")
    print(f"profile    {'set' if store.has_profile() else 'empty'}")
    for label, rows in (("companies", store.get_companies()),
                        ("answers", store.get_answers()),
                        ("applied", store.get_applied()),
                        ("outreach", store.get_outreach()),
                        ("pending", store.all_pending()),
                        ("submitted", store.all_pending(archived=True)),
                        ("resumes", store.all_resumes()),
                        ("queue", store.get_queue("queue"))):
        print(f"{label:10} {len(rows)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.store", description=__doc__)
    ap.add_argument("--url", help="database URL; defaults to DATABASE_URL or the local file")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("import", help="read the file layout into the database").set_defaults(
        fn=cmd_import)

    ex = sub.add_parser("export", help="write the database back out")
    ex.add_argument("directory", nargs="?", help="where to write the whole layout")
    for name in SINGLE:
        ex.add_argument(f"--{name}", action="store_true", help=f"print {SINGLE[name][1]}")
    ex.set_defaults(fn=cmd_export)

    sub.add_parser("migrate", help="bring the schema to head").set_defaults(fn=cmd_migrate)
    sub.add_parser("status", help="row counts").set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
