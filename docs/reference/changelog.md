# Changelog

Reclaimerr can expose a changelog through the API at `GET /api/info/changelog`.
That endpoint reads a `CHANGELOG.md` file from the running environment and
splits it into release entries.

## Format

The parser looks for headings like:

```markdown
## [0.1.0] - 2026-06-02
```

It also accepts an `Unreleased` entry:

```markdown
## [Unreleased]
```

## API Response

Each entry is returned as a JSON object with:

- `version`
- `date`
- `body`

If the changelog file is missing, the endpoint returns `404`.

## Usage

- Keep unreleased changes at the top of `CHANGELOG.md`.
- Use one heading per release.
- Keep the body in plain Markdown.

## Related Pages

- [External API](api.md)
- [Internal UI API](internal-api.md)
- [Troubleshooting](troubleshooting.md)
- [Release Process](../development/release-process.md)
