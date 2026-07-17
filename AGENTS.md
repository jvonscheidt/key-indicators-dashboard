## Project context

See `README.md` for scope, the indicator list, architecture, and the
FR-*/NFR-*/§ requirement IDs that code comments cite throughout `data/`
and `app.py`.

`.streamlit/secrets.toml` holds the local `FRED_API_KEY` and is
gitignored — never commit it or paste its contents into a commit message
or PR description.

## Git & GitHub

- Default branch is `main`; minor changes commit to it directly. Major changes get branched:
  `feat/<topic>`, `fix/<topic>`, `chore/<topic>`.
- Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
  `chore:`. Imperative subject ≤ 72 chars; body explains *why*, not *what*.
- One logical change per commit; run Black, Ruff, and pytest before committing.
- Update branches with `git pull --rebase`; never force-push a shared branch.
- Use the `gh` CLI for GitHub work (`gh pr create`, `gh issue view`, …). Keep
  PRs small and single-purpose; CI must be green before merge.
- Update the README before tagging a new version.
