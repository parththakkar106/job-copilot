"""Shared pipeline modules: the scraper, the profile/taxonomy resolver, the answer bank,
the resume cache and the applied-log helper.

These are imported as `core.<name>` by the bridge and the web console, and are also still
runnable directly (`python core/scraper.py`) — each one puts the repo root on sys.path so
both entry paths resolve `core.paths` the same way. See core/paths.py for the layout.
"""
