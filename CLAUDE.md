# CLAUDE.md

Project rules for this repository live in **[AGENTS.md](AGENTS.md)** — read and
follow them on every change. They are mandatory, not advisory.

@AGENTS.md

In short: SOLID/DRY readable code; no module over 500 lines (refactor if it
grows); write/update/verify tests (`python -m unittest discover -s tests -v`)
for every source change; after each change dispatch ≥2 high-effort reviewer
subagents; and verify behaviour across Windows, Linux, and macOS.
