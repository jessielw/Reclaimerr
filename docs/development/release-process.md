# Release Process

Reclaimerr does not have a fully automated release pipeline in-repo. Treat releases as a deliberate maintainer action with a short checklist.

## Before a Release

- Run `uv run python scripts/bump_version.py <version>` (or `patch`/`minor`/`major`) to bump every version file.
- Update `CHANGELOG.md`: move the `Unreleased` entries under a `## [X.Y.Z] - YYYY-MM-DD` heading for stable releases. Betas keep using `Unreleased`.
- Run the test and lint commands you normally use for the code that changed.
- Confirm the docs still build with `uv run zensical build --clean`.

## Release Sequence

1. Merge the release-ready changes to the target branch.
2. Run `scripts/bump_version.py` and update `CHANGELOG.md`.
3. Run validation locally.
4. Commit and push, then publish the GitHub release with a new tag (`X.Y.Z` or `X.Y.Z-beta.N`). Leave the release description empty.
5. The tag push starts the Docker and desktop builds. The `Release Notes` workflow fills the description (see below).

## Versioning Notes

- `backend/core/__version__.py` is the Python version source. `pyproject.toml` reads it.
- `frontend/package.json` is the frontend version source. `frontend/src/lib/version.ts` reads it.
- The release notes endpoint reads `CHANGELOG.md` from the runtime environment.

## Release Notes Workflow

`.github/workflows/release-notes.yml` runs when a release is published:

- Stable tags (`0.6.2`) use the matching `## [0.6.2]` section of `CHANGELOG.md`.
- Prerelease tags (`0.6.2-beta.1`) use the `## [Unreleased]` section.
- GitHub's generated "What's Changed" and "Full Changelog" are appended below the changelog entries.
- The description starts with `# Wait to grab - release is building (until this is removed)`. The workflow removes that line once both `Docker Build` and `Desktop Build` succeed for the tag. If a build fails, the line stays.
- If the changelog section is missing or empty, the description says so. Edit it by hand.
- A release that already has a description is left alone.
- The workflow reads `CHANGELOG.md` from the tagged commit, so commit the changelog before tagging.

## After the Release

- Verify the published version is visible in the app and `/api/version`.
- Check that the changelog endpoint still returns the expected release entries.
- Update any deployment notes if the release changes install or upgrade steps.

## Related Pages

- [Changelog](../reference/changelog.md)
- [External API](../reference/api.md)
- [Internal UI API](../reference/internal-api.md)
- [Contributing](contributing.md)
