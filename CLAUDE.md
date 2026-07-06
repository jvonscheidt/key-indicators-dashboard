## Python environment

This project's virtual environment lives at `.venv/`. Invoke its
interpreter directly; do not run `source .venv/bin/activate` (it cannot
persist between Claude's tool calls).

Use these commands:

- Run a script: `.venv/bin/python script.py`
- Open a REPL: `.venv/bin/python`
- Install a package: `.venv/bin/pip install <package>`
- Run a tool: `.venv/bin/pytest`, `.venv/bin/ruff check`,
  `.venv/bin/mypy`

Each entry under `.venv/bin/` resolves to the venv's copy of the tool,
so Python sees the project's `site-packages` automatically.

## Git & GitHub

- Default branch is `main`; minor changes commit to it directly. Major changes get branched:
  `feat/<topic>`, `fix/<topic>`, `chore/<topic>`.
- Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
  `chore:`. Imperative subject ≤ 72 chars; body explains *why*, not *what*.
- One logical change per commit; run the lint/format/test before.
- Update branches with `git pull --rebase`; never force-push a shared branch.
- Use the `gh` CLI for GitHub work (`gh pr create`, `gh issue view`, …). Keep
  PRs small and single-purpose; CI must be green before merge.
- Update the README before tagging a new version.