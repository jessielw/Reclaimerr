# Rules

Cleanup rules are built from nested groups of conditions. Each condition has a
field, an operator, and a value when the operator needs one.

## Operator Labels

The UI uses these labels for the list-matching operators:

| Operator | Label | Meaning |
| --- | --- | --- |
| `contains_any` | matches any | At least one listed value must match |
| `not_contains_any` | matches none | None of the listed values may match |
| `contains_all` | matches all | Every listed value must match |
| `not_contains_all` | does not match all | Not every listed value may match |

These are label-only changes. Existing rules do not need to be adjusted.

## Validation and Editing

- The rule editor keeps operator choices scoped to the selected field.
- If a field changes and the current operator is no longer valid, Reclaimerr
  falls back to the field's default operator.
- Preview and validation endpoints still enforce rule shape and field scope.
- This fallback only affects how the rule is edited going forward; existing
  saved rules are not rewritten unless you save them again.

## Example

For a list field such as `tmdb.genres` or `media_server.collections`:

- **matches any** is the broadest match
- **matches none** is the strict inverse
- **matches all** requires every listed item
- **does not match all** means at least one listed value does not match

## Related Pages

- [How It Works](how-it-works.md)
- [Tasks](tasks.md)
