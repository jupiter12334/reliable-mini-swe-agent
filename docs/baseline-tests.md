# Original mini-swe-agent offline test baseline

This report records the offline test baseline before Reliable-MiniSWE changes
the agent runtime. It is intended to make later regression and comparison
results reproducible.

## Baseline identity

- Upstream baseline commit: `25941c89`
- Tested development commit: `c8582498`
- Source comparison: no differences under `src/`, `tests/`, or
  `pyproject.toml` between the two commits
- mini-swe-agent: `2.4.6`
- Lockfile SHA-256:
  `3e4faabf5f51f70aad09fbc4de921d6dab1c07d33701a678f23414a4689809a4`

The development commit contains fork metadata and workflow documentation, but
does not change the upstream Python implementation or tests.

## Environment

- Date: 2026-09-04
- Operating system: macOS 26.6.2
- Architecture: arm64
- Python: CPython 3.13.13
- pytest: 9.1.1
- Environment manager: uv

## Reproduction

Run the commands from the repository root:

```bash
uv sync --extra dev --group dev
uv run pytest -q -m "not slow" --ignore=tests/environments/extra
```

The `dev` optional extra is required because model-adapter tests import
development-only packages such as `portkey-ai`. Synchronizing only the `dev`
dependency group is insufficient for this test scope.

## Scope

This is a credential-free core offline baseline:

- Tests marked `slow` are deselected.
- `tests/environments/extra` is ignored because it covers optional Contree,
  SWE-ReX, Modal, and other external execution environments.
- No paid model request is required by the selected test scope.
- Environment-dependent tests may skip themselves when their local runtime is
  unavailable.

The official full CI suite has a broader purpose and installs the `full` extra
plus Linux execution tools. It should be treated as a separate compatibility
check, not as this laptop's core offline baseline.

## Result

```text
514 passed, 14 skipped, 58 deselected, 1 warning in 36.33s
```

- Failures: 0
- Collection errors: 0 after installing the documented development extras
- Warning: one existing deprecation warning for
  `last_n_messages_offset` in `models/utils/cache_control.py`

## Initial dependency finding

The first attempt used an environment without the `dev` optional extra and
stopped during collection because `portkey-ai` was missing. Running
`uv sync --extra dev --group dev` installed the declared development adapters;
the unchanged test command then passed. This was an environment preparation
issue, not a product test failure.
