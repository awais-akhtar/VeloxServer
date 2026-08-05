# Publishing VeloxServer

This page is for maintainers preparing a VeloxServer release. End users should install VeloxServer from PyPI:

```bash
python -m pip install veloxserver
```

## Release Checklist

Update the version in:

```text
pyproject.toml
src/veloxserver/__init__.py
README.md
CHANGELOG.md
```

Run syntax checks.

Linux and macOS:

```bash
python -B -m py_compile src/veloxserver/ai.py src/veloxserver/auth.py src/veloxserver/cli.py src/veloxserver/config.py src/veloxserver/deploy_ai.py src/veloxserver/http3.py src/veloxserver/native.py src/veloxserver/plugins.py src/veloxserver/repair.py src/veloxserver/server.py src/veloxserver/shared.py src/veloxserver/stream.py src/veloxserver/workers.py tests/test_server.py benchmarks/static_benchmark.py benchmarks/run_controlled_matrix.py
```

Windows PowerShell:

```powershell
python -B -m py_compile src\veloxserver\ai.py src\veloxserver\auth.py src\veloxserver\cli.py src\veloxserver\config.py src\veloxserver\deploy_ai.py src\veloxserver\http3.py src\veloxserver\native.py src\veloxserver\plugins.py src\veloxserver\repair.py src\veloxserver\server.py src\veloxserver\shared.py src\veloxserver\stream.py src\veloxserver\workers.py tests\test_server.py benchmarks\static_benchmark.py benchmarks\run_controlled_matrix.py
```

Run tests:

```bash
python -B -c "import sys, unittest; sys.path.insert(0, 'src'); suite = unittest.defaultTestLoader.discover('tests'); result = unittest.TextTestRunner(verbosity=2).run(suite); raise SystemExit(0 if result.wasSuccessful() else 1)"
```

Run the fuzz smoke campaign.

Linux and macOS:

```bash
python -B fuzz/run_fuzz_campaign.py
```

Windows PowerShell:

```powershell
python -B fuzz\run_fuzz_campaign.py
```

Run compatibility and benchmark helpers for the release target:

```bash
python tools/http2_stress_matrix.py --help
python tools/http3_compat_matrix.py --help
python tools/tls_probe_matrix.py --help
python benchmarks/run_controlled_matrix.py --help
```

## Build Artifacts

Install build tools:

```bash
python -m pip install --upgrade build twine
```

Clean local artifacts.

Linux and macOS:

```bash
rm -rf build dist src/*.egg-info
```

Windows PowerShell:

```powershell
Remove-Item -Recurse -Force build,dist,src\*.egg-info -ErrorAction SilentlyContinue
```

Build and validate:

```bash
python -m build
python -m twine check dist/*
```

## Native Wheels

The default PyPI release uploads a source distribution and a pure Python wheel. It works without a bundled Rust library.

Native acceleration requires platform-specific shared libraries in `src/veloxserver/native_libs`. Binary wheels are built only when native libraries are present or `VELOXSERVER_BINARY_WHEEL=1` is set.

The wheel workflow builds the Rust core on Linux, macOS, and Windows, copies the shared library into the package, and uploads wheel artifacts. Linux binary wheels need manylinux or musllinux repair before publishing to PyPI.

Manual Rust build on Linux and macOS:

```bash
cd native/rust
cargo build --release
cd ../..
```

Manual Rust build on Windows PowerShell:

```powershell
Set-Location native\rust
cargo build --release
Set-Location ..\..
```

Expected library names:

```text
Linux:   native/rust/target/release/libveloxcore.so
macOS:   native/rust/target/release/libveloxcore.dylib
Windows: native/rust/target/release/veloxcore.dll
```

## PyPI Trusted Publishing

VeloxServer is configured for PyPI Trusted Publishing through GitHub Actions. This avoids long-lived PyPI API tokens.

For the first upload to a new or recreated PyPI project, open your PyPI account **Publishing** page and add a pending GitHub Trusted Publisher with these values:

```text
PyPI Project Name: veloxserver
Owner: awais-akhtar
Repository name: VeloxServer
Workflow name: release.yml
Environment name: pypi
```

After the first successful upload, PyPI converts the pending publisher into the normal project publisher for `veloxserver`.

The matching workflow lives at `.github/workflows/release.yml`.

Create a GitHub environment named `pypi` in the repository settings before publishing. Manual approval on that environment is recommended for public releases.

After the publisher is saved on PyPI, pushing a version tag such as `v0.1.0` or manually running the `release` workflow will build the package and publish it to PyPI.

## PyPI

Trusted publishing is the release path. Create a tag and let GitHub Actions publish with OIDC:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The `release` workflow can also be started manually from GitHub Actions with `workflow_dispatch`.

Verify after the release completes:

```bash
python -m pip install --upgrade veloxserver
veloxserver --version
```

## GitHub Release

Create a GitHub release from the tagged commit in the hosted repository. The tag starts the wheel workflow. Download and test wheel artifacts before attaching them to the release or publishing them to PyPI.

Release notes should include:

- headline feature changes
- compatibility notes
- security notes
- new config keys
- known limitations
- test, fuzz, HTTP/2, HTTP/3, TLS, and benchmark evidence

Do not claim production maturity, independent audit completion, or broad HTTP/3/TLS compatibility until the release has evidence for those claims.

## Clean Public Repository

The `.gitignore` file excludes build output, virtual environments, caches, logs, model files, local certificates, database files, repair artifacts, and native binaries.

Before pushing, review the staged files:

```bash
git status --short
```

Only source files, docs, tests, examples, deployment templates, native source, workflows, and package metadata should be committed.

If secrets, private code, or unwanted files were already pushed to a public repository, do not rely on history rewriting as a security boundary. Rotate affected credentials, remove the public exposure where possible, and use GitHub's sensitive-data removal guidance. A fresh public repository can provide a clean first-release history, but it cannot make old forks, clones, caches, or downloaded copies disappear.
