# Rule Format

This page describes the JSON file that **Rules → Export** writes and **Rules → Import** reads. Use it to write rules by hand, review a shared rule before importing it, or give an AI assistant enough context to draft rules for you.

The full list of condition fields, with the operators and scopes each one accepts, is on [Rule Fields](rule-fields.md). That page is generated from the rule engine, so it always matches the running version.

For what rules do and how to build them in the editor, see [Rules](../usage/rules.md).

## File shape

An import file is a JSON array of rule objects. A single rule object without the array is also accepted.

```json
[
  {
    "name": "Old unwatched movies",
    "description": "Movies added more than a year ago that nobody has watched.",
    "media_type": "movie",
    "enabled": false,
    "target_scope": "movie_version",
    "definition": {
      "version": 1,
      "root": {
        "type": "group",
        "op": "and",
        "children": [
          {
            "type": "condition",
            "field": "media.days_since_added",
            "operator": "greater_than",
            "value": 365
          },
          {
            "type": "condition",
            "field": "watch.never_watched",
            "operator": "is_true"
          }
        ]
      }
    },
    "action": {
      "outcome": "candidate",
      "arr_action": "delete",
      "tag_enabled": false,
      "auto_delete_enabled": false
    }
  }
]
```

## Rule object

| Key | Required | Value |
| --- | --- | --- |
| `name` | Yes | Display name. A name that already exists is imported as `Name (imported)`. |
| `description` | No | Plain-text notes shown with the rule. |
| `media_type` | Yes | `"movie"` for `movie_version` rules, `"series"` for every other scope. |
| `enabled` | No | Defaults to `true`. Use `false` so the rule can be previewed before it runs. |
| `target_scope` | Yes | `"movie_version"`, `"series"`, `"season"`, or `"episode"`. |
| `definition` | Yes | The condition tree. See below. |
| `action` | No | What happens to matches. Omitted keys use the defaults below. |

Exported files can also contain `id`, `created_at`, and `updated_at`. Import ignores them.

### Target scopes

| Scope           | One match is                                               |
| --------------- | ---------------------------------------------------------- |
| `movie_version` | One movie file. A movie with two files is evaluated twice. |
| `series`        | A whole show.                                              |
| `season`        | One season of a show.                                      |
| `episode`       | One episode.                                               |

Each field is only allowed in some scopes. Check the **Scopes** column on [Rule Fields](rule-fields.md).

## Definition

```json
{
  "version": 1,
  "root": { "type": "group", "op": "and", "children": [] }
}
```

`version` is always `1`. `root` is a group.

### Group

| Key | Value |
| --- | --- |
| `type` | `"group"` |
| `op` | `"and"` (every child must match) or `"or"` (at least one child must match) |
| `children` | Non-empty array of groups and conditions. Groups can nest. |
| `enabled` | Optional. `false` turns the group off without deleting it. |

### Condition

| Key | Value |
| --- | --- |
| `type` | `"condition"` |
| `field` | A field key from [Rule Fields](rule-fields.md), such as `media.size` |
| `operator` | An operator the field accepts |
| `value` | The comparison value. Omit it for `exists`, `not_exists`, `is_true`, and `is_false`. |
| `enabled` | Optional. `false` turns the condition off without deleting it. |

A rule needs at least one enabled condition.

### Operators

| Operator | `value` |
| --- | --- |
| `equals`, `not_equals` | One number or string |
| `greater_than`, `greater_than_or_equal`, `less_than`, `less_than_or_equal` | One number |
| `before`, `on_or_before`, `after`, `on_or_after` | One ISO date, such as `"2024-01-31"` |
| `in`, `not_in`, `contains_any`, `not_contains_any`, `contains_all`, `not_contains_all` | Array of strings |
| `contains_substring`, `not_contains_substring` | Array of strings (Arr tags only) |
| `matches_any_regex`, `not_matches_any_regex` | Array of regex patterns, matched case-insensitively |
| `exists`, `not_exists`, `is_true`, `is_false` | None. Omit `value`. |

Text comparisons ignore case.

### Values that need care

- **Sizes** (`media.size`, `disk.free_bytes`) are bytes, using 1024-based units. 50 GiB is `53687091200`.
- **Duration** (`media.duration`) is milliseconds.
- **Ratings** have fixed ranges. Values outside them are rejected; see the **Value notes** column on [Rule Fields](rule-fields.md).
- **Per-user playback** fields (`playback.user_watched_duration_minutes`, `playback.user_watched_percent`) take an object: `{"usernames": ["alice"], "amount": 90}`.
- **Seerr requester IDs** are written `"instanceId:userId"`, such as `"7:3"`.

### Install-specific values

Some fields and action keys refer to IDs or names that only exist on one Reclaimerr install. A rule that uses them will import, but it must be edited before it matches the right items elsewhere.

- Fields: `library.id`, `arr.movie_ids`, `arr.series_ids`, `seerr.requested_by_user_ids`, `playback.usernames`, `playback.fully_watched_usernames`, `favorites.usernames`, and the per-user playback fields.
- Action keys: `radarr_service_config_ids`, `sonarr_service_config_ids`, and `quality_profile_id`.

A rule without a `library.id` condition covers every library of its media type. To limit it to some libraries, add the library in the rule editor after import.

## Action

| Key | Default | Value |
| --- | --- | --- |
| `outcome` | `"candidate"` | `"candidate"` makes matches cleanup candidates. `"protect"` protects matches and ignores the rest of the action. `"match"` takes no action and ignores the rest of the action; matches are read through the [External API](api.md#rules). |
| `arr_action` | `"delete"` | `"delete"`, `"unmonitor"`, `"unmonitor_only"`, `"unmonitor_delete_monitor_new_seasons"`, `"unmonitor_delete_list_exclusion"`, `"unmonitor_delete_monitor_or_exclude"`, or `"change_quality_profile"` |
| `tag_enabled` | `true` | Tag matches in Radarr/Sonarr |
| `arr_tag` | rule name | Tag text. Converted to a `rec-` slug. |
| `auto_delete_enabled` | `false` | Run the action automatically when the review period ends |
| `auto_delete_delay_days` | `null` | Review period override, `0` to `3650`. `null` uses the global default. |
| `move_instead_of_delete` | `false` | Move files to the configured destination instead of deleting them |
| `quality_profile_id` | `null` | Required for `change_quality_profile`. Install-specific. |
| `trigger_search` | `true` | Search after a quality profile change |
| `radarr_service_config_ids`, `sonarr_service_config_ids` | `[]` | Limit the action to these Arr instances. Install-specific. |

Arr action limits:

- `unmonitor_delete_monitor_new_seasons` and `unmonitor_delete_monitor_or_exclude` are only allowed on `season` rules.
- `unmonitor_delete_list_exclusion` is allowed on `movie_version` and `season` rules. Sonarr requires an ended show and removal of the latest regular season before adding an exclusion.
- `change_quality_profile` is only allowed on `movie_version` and `series` rules, and needs `quality_profile_id`.

See [Arr Action](../usage/rules.md#arr-action) for what each action does.

## Examples

### Large, low-rated movies

```json
[
  {
    "name": "Large, low-rated movies",
    "description": "Movie files over 30 GiB with an IMDb rating under 6.",
    "media_type": "movie",
    "enabled": false,
    "target_scope": "movie_version",
    "definition": {
      "version": 1,
      "root": {
        "type": "group",
        "op": "and",
        "children": [
          {
            "type": "condition",
            "field": "media.size",
            "operator": "greater_than",
            "value": 32212254720
          },
          {
            "type": "condition",
            "field": "imdb.rating",
            "operator": "less_than",
            "value": 6
          }
        ]
      }
    },
    "action": { "outcome": "candidate", "arr_action": "delete" }
  }
]
```

### Ended shows nobody has played in a year

```json
[
  {
    "name": "Stale ended shows",
    "description": "Whole shows Sonarr marks ended, with no playback activity for 365 days or no recorded playback at all.",
    "media_type": "series",
    "enabled": false,
    "target_scope": "series",
    "definition": {
      "version": 1,
      "root": {
        "type": "group",
        "op": "and",
        "children": [
          {
            "type": "condition",
            "field": "sonarr.series_status",
            "operator": "equals",
            "value": "ended"
          },
          {
            "type": "group",
            "op": "or",
            "children": [
              {
                "type": "condition",
                "field": "playback.days_since_last_activity",
                "operator": "greater_than",
                "value": 365
              },
              {
                "type": "condition",
                "field": "playback.has_activity",
                "operator": "is_false"
              }
            ]
          }
        ]
      }
    },
    "action": { "outcome": "candidate", "arr_action": "unmonitor" }
  }
]
```

### Protect highly rated movies

```json
[
  {
    "name": "Protect highly rated movies",
    "description": "Never offer movies rated 8 or higher on IMDb for cleanup.",
    "media_type": "movie",
    "enabled": false,
    "target_scope": "movie_version",
    "definition": {
      "version": 1,
      "root": {
        "type": "group",
        "op": "and",
        "children": [
          {
            "type": "condition",
            "field": "imdb.rating",
            "operator": "greater_than_or_equal",
            "value": 8
          }
        ]
      }
    },
    "action": { "outcome": "protect" }
  }
]
```

## What import checks

Each rule in the file is checked on its own. A rule that fails is reported with its name and the reason, and the other rules still import.

Import rejects a rule when:

- `target_scope` is missing or unknown
- a field or operator is unknown, or the operator is not allowed for that field
- a field is not allowed in the rule's scope
- a required `value` is missing, or a number is outside the field's range
- a regex or path value cannot be parsed
- the action breaks an Arr action limit

Import does not check that a date string is a valid date, or that install-specific IDs exist. A wrong value there imports cleanly and never matches. Use **Preview Matches** in the rule editor before enabling an imported rule.

## Writing rules with an AI assistant

1. Give the assistant this page and [Rule Fields](rule-fields.md), either as links or pasted text.
2. Describe the rule in plain words: what to match, whether it is a cleanup or protection rule, and the scope (movies, shows, seasons, or episodes).
3. Ask for a JSON array in the format above with `"enabled": false`.
4. Save the reply as a `.json` file and import it with **Rules → Import**. Fix any errors the import reports.
5. Open the rule, add library or user selections if needed, and check **Preview Matches** before enabling it.

## Sharing rules

Rules other users have shared are on [Community Rules](../community-rules/index.md), along with how to add your own.
