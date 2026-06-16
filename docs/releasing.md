# Releasing

bi-evals uses **manual versioning with a hand-maintained changelog** and
**annotated git tags**. Version flow is intentionally simple while the project is
early-stage and solo.

## Versioning scheme

[Semantic Versioning](https://semver.org/). Pre-1.0, so:

- **`0.x.0` (minor)** — new features; **may include breaking changes** (config
  schema, SDK signatures). Call breaks out explicitly in the changelog.
- **`0.0.x` (patch)** — fixes and docs only, no API/config change.
- `1.0.0` — reserved for when the SDK + config schema are committed to as stable.

## Single source of truth

The version lives in **one place**: `__version__` in `src/bi_evals/__init__.py`.
`pyproject.toml` reads it dynamically (`[tool.hatch.version]`), so there is
nothing to keep in sync — edit `__init__.py` only.

```python
# src/bi_evals/__init__.py
__version__ = "0.1.0"
```

## Cutting a release

1. **Pick the version** per the scheme above (look at what's under
   `## [Unreleased]` in `CHANGELOG.md` to decide minor vs. patch).
2. **Bump** `__version__` in `src/bi_evals/__init__.py`.
3. **Update `CHANGELOG.md`**: rename the `## [Unreleased]` section to
   `## [X.Y.Z] - YYYY-MM-DD`, add a fresh empty `## [Unreleased]` above it, and
   update the link references at the bottom.
4. **Verify** the version resolves:
   ```bash
   uv run python -c "import bi_evals; print(bi_evals.__version__)"
   ```
5. **Commit**: `git commit -am "release: vX.Y.Z"`
6. **Tag (annotated)**:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin main --follow-tags
   ```

## Changelog discipline

Add entries under `## [Unreleased]` **as features land**, not at release time —
grouped under `Added` / `Changed` / `Fixed` / `Breaking`. The release step then
just stamps a date on that section. Commit messages already follow Conventional
Commits (`feat:` / `fix:` / `docs:`), which makes assembling the changelog from
`git log` straightforward if an entry is ever missed.

## Why not automated release tooling (yet)

release-please / semantic-release are a good fit once there's a team and a
release cadence. For a solo, pre-1.0 project the manual flow is less machinery to
maintain and keeps full control over what ships in each tag. Revisit when releases
become frequent or multiple contributors are merging.
