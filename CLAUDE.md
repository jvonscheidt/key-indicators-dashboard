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
