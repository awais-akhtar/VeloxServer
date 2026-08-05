# Contributing To VeloxServer

Thanks for helping improve VeloxServer. Contributions are welcome for core server behavior, docs, tests, deployment examples, benchmarks, fuzzing, AI deployment tooling, plugins, and native-core work.

## Development Setup

Clone the repository from GitHub, then install it in editable mode.

Linux and macOS:

```bash
cd veloxserver
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[ai-repair]"
```

Windows PowerShell:

```powershell
cd veloxserver
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[ai-repair]"
```

## Run Tests

Linux and macOS:

```bash
python -B -c "import sys, unittest; sys.path.insert(0, 'src'); suite = unittest.defaultTestLoader.discover('tests'); result = unittest.TextTestRunner(verbosity=2).run(suite); raise SystemExit(0 if result.wasSuccessful() else 1)"
python -B fuzz/run_fuzz_campaign.py
```

Windows PowerShell:

```powershell
python -B -c "import sys, unittest; sys.path.insert(0, 'src'); suite = unittest.defaultTestLoader.discover('tests'); result = unittest.TextTestRunner(verbosity=2).run(suite); raise SystemExit(0 if result.wasSuccessful() else 1)"
python -B fuzz\run_fuzz_campaign.py
```

## Build Package Artifacts

Linux and macOS:

```bash
python -m pip install build twine
python -m build
python -m twine check dist/*
```

Windows PowerShell:

```powershell
python -m pip install build twine
python -m build
python -m twine check dist/*
```

## Pull Request Expectations

- Keep changes focused and explain the user-facing behavior.
- Add or update tests when changing server behavior.
- Update docs for new commands, config keys, deployment behavior, or AI features.
- Do not include secrets, local certificates, model files, logs, cache files, or generated repair artifacts.
- Do not copy code or config language from other servers.

## Feature Areas

Useful contribution areas:

- HTTP/2 and HTTP/3 compatibility testing
- reverse proxy edge cases
- deployment examples for real frameworks
- benchmark scripts and reproducible results
- AI deployment assistant improvements
- AI error repair safety and diagnostics
- plugin examples
- native Rust hot-path expansion
- docs and onboarding polish

Maintainer release steps are documented in [docs/publishing.md](docs/publishing.md).
