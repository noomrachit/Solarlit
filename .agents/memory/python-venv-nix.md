---
name: Python virtualenv in Nix
description: How to run Python bots/scripts in the Replit NixOS environment when pip install fails
---

The Nix environment blocks system-wide `pip install` (externally-managed-environment error). A global `pip.conf` also forces `--user` mode, which fails inside a venv.

**Rule:** Always create a venv AND pass `--no-user`:
```
python3 -m venv .venv && .venv/bin/pip install -q --no-user -r requirements.txt && .venv/bin/python script.py
```

**Why:** NixOS marks the Python install as externally managed (PEP 668). The venv escapes that restriction. The `--no-user` flag overrides the global pip.conf that would otherwise inject `--user`, which is incompatible with venvs.

**How to apply:** Any new Python workflow command in this project must follow this pattern. Add `.venv/` to the directory's `.gitignore`.
