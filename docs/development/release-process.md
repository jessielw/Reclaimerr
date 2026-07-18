# Release Process

Reclaimerr does not have a fully automated release pipeline in-repo. Treat
releases as a deliberate maintainer action with a short checklist.

## Before a Release

- Make sure the version matches in both `pyproject.toml` and
  `backend/core/__version__.py`.
- Update `CHANGELOG.md` with an `Unreleased` section or the final release notes.
- Run the test and lint commands you normally use for the code that changed.
- Confirm the docs still build with `uv run zensical build --clean`.

## Release Sequence

1. Merge the release-ready changes to the target branch.
2. Bump the version in the project metadata and runtime version module.
3. Update `CHANGELOG.md`.
4. Run validation locally.
5. Create the git tag for the release.
6. Publish the GitHub release and attach any build artifacts you ship.

## Versioning Notes

- `pyproject.toml` is the packaging version source.
- `backend/core/__version__.py` is the runtime version source used by the app.
- The release notes endpoint reads `CHANGELOG.md` from the runtime environment.

## After the Release

- Verify the published version is visible in the app and `/api/version`.
- Check that the changelog endpoint still returns the expected release entries.
- Update any deployment notes if the release changes install or upgrade steps.

## Related Pages

- [Changelog](../reference/changelog.md)
- [External API](../reference/api.md)
- [Internal UI API](../reference/internal-api.md)
- [Contributing](contributing.md)
