# Scripting Arr Changes

Reclaimerr doesn't change Radarr or Sonarr settings such as quality profiles, root folders, tags, or monitoring for maintenance. Instead, a **match-only rule** finds the media, and a script you control asks the [External API](../reference/api.md#rules) for the matches and makes the change in Radarr or Sonarr itself.

```text
match-only rule  ->  GET /api/v1/rules/{id}/matches  ->  your script  ->  Radarr / Sonarr API
```

A match-only rule never creates candidates or protections, and scans skip it. It is evaluated only when your script asks.

!!! tip
    If all you need is a quality profile change with a review period, a cleanup rule's **Change Quality Profile** action does that without a script. See [Arr Action](rules.md#arr-action).

## Set Up

### 1. Create the Rule

1. Open **Rules** and add a rule.
2. Set **Outcome** to **Match only**.
3. Add the conditions, then use **Preview Matches** to check the list.
4. Add a guard condition so the rule stops matching once your script has acted. See [Make Reruns Harmless](#make-reruns-harmless).
5. Enable the rule and save it.

### 2. Create a Token

Open **Settings > Integrations** and create an API token with only the `rules:read` scope. Copy it; it is shown once.

```bash
export RECLAIMERR="https://reclaimerr.example"
export RECLAIMERR_TOKEN="rcl_prefix_secret"
```

### 3. Find the IDs Your Script Needs

The rule ID:

```bash
curl -fsS -H "Authorization: Bearer $RECLAIMERR_TOKEN" "$RECLAIMERR/api/v1/rules" \
  | jq '.items[] | select(.outcome == "match") | {id, name, enabled}'
```

The Arr instance IDs. With more than one Radarr or Sonarr, each match lists every instance that has the title, so pick one:

```bash
curl -fsS -H "Authorization: Bearer $RECLAIMERR_TOKEN" "$RECLAIMERR/api/v1/rules/12/matches" \
  | jq '[.items[].arr[]] | unique_by(.service_config_id) | map({service, service_config_id, service_name})'
```

Quality profile, tag, and root folder IDs come from Radarr or Sonarr. The same paths work on both:

```bash
export RADARR="http://radarr:7878"
export RADARR_API_KEY="..."

curl -fsS -H "X-Api-Key: $RADARR_API_KEY" "$RADARR/api/v3/qualityprofile" | jq 'map({id, name})'
curl -fsS -H "X-Api-Key: $RADARR_API_KEY" "$RADARR/api/v3/rootfolder" | jq 'map(.path)'
curl -fsS -H "X-Api-Key: $RADARR_API_KEY" "$RADARR/api/v3/tag" | jq 'map({id, label})'

# create a tag if you need a new one
curl -fsS -X POST -H "X-Api-Key: $RADARR_API_KEY" -H "Content-Type: application/json" \
  -d '{"label":"reprofiled"}' "$RADARR/api/v3/tag"
```

## The Response

```json
{
  "rule": { "id": 12, "name": "Unwatched 4K movies", "media_type": "movie", "target_scope": "movie_version" },
  "complete": true,
  "warnings": [],
  "total": 1,
  "items": [
    {
      "target_scope": "movie_version",
      "media_type": "movie",
      "movie_id": 81,
      "movie_version_id": 140,
      "series_id": null,
      "season_number": null,
      "episode_number": null,
      "title": "Fight Club",
      "year": 1999,
      "tmdb_id": 550,
      "tvdb_id": null,
      "imdb_id": "tt0137523",
      "arr": [
        { "service": "radarr", "service_config_id": 1, "service_name": "Radarr 4K", "arr_id": 42 }
      ],
      "reason": "..."
    }
  ]
}
```

`arr_id` is the movie or series ID inside that Radarr or Sonarr instance, so you can pass it straight to its API. Season and episode rules return the Sonarr series ID plus `season_number` and `episode_number`. `reason` is a readable summary of why the item matched, for logs; don't parse it. Every field is described in the [External API reference](../reference/api.md#rules).

## Safety Rules

Every example below follows these rules. Keep them in your own scripts.

1. **Skip the run when `complete` is `false`.** Missing provider data counts as no match, so the list may be partial. `warnings` says what was missing.
2. **Act on one instance.** Filter `arr` by `service_config_id`.
3. **Remove duplicates.** A movie with two matching versions appears twice, and season and episode rules repeat the series. Collect IDs with `unique`.
4. **Dry run first.** Each example prints what it would change when `DRY_RUN=1` is set.
5. **Disable the rule to stop the script.** The API returns HTTP 409 for a disabled rule, and the examples exit with an error instead of acting.

### Make Reruns Harmless

Add a condition that stops matching once the change is made, so running the script again does nothing to items it already handled:

| Change | Guard condition |
| --- | --- |
| Add a tag | **Arr tags** does not contain the tag |
| Unmonitor | **Arr monitored** is true |
| Move root folder | **Arr tags** does not contain a tag the script also adds |

Reclaimerr reads Arr tags and monitoring from Radarr and Sonarr each time the rule is evaluated, so the guard works on the very next run.

## Examples

### Radarr: Re-profile Unwatched 4K Movies

Moves 4K movies nobody has watched in a year onto a smaller quality profile and tags them.

Rule (movie version scope, match only):

- **Video height** greater than or equal to `2000`
- **Days since watched** greater than `365`, or **Never watched** is true and **Days since added** greater than `365`
- **Arr tags** does not contain `reprofiled`

```bash
#!/usr/bin/env bash
set -euo pipefail

RULE_ID=12
RADARR_CONFIG_ID=1   # service_config_id of this Radarr in Reclaimerr
PROFILE_ID=4         # target quality profile in Radarr
TAG_ID=3             # the "reprofiled" tag in Radarr

matches=$(curl -fsS -H "Authorization: Bearer $RECLAIMERR_TOKEN" \
  "$RECLAIMERR/api/v1/rules/$RULE_ID/matches")

if [ "$(jq -r '.complete' <<<"$matches")" != "true" ]; then
  echo "Incomplete data, skipping:" >&2
  jq -r '.warnings[]' <<<"$matches" >&2
  exit 1
fi

movie_ids=$(jq -c --argjson cfg "$RADARR_CONFIG_ID" \
  '[.items[].arr[] | select(.service == "radarr" and .service_config_id == $cfg) | .arr_id] | unique' \
  <<<"$matches")

if [ "$movie_ids" = "[]" ]; then
  echo "Nothing to change"
  exit 0
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "Would re-profile Radarr movies: $movie_ids"
  jq -r '.items[] | "  \(.title) (\(.year))"' <<<"$matches"
  exit 0
fi

curl -fsS -X PUT -H "X-Api-Key: $RADARR_API_KEY" -H "Content-Type: application/json" \
  -d "{\"movieIds\":$movie_ids,\"qualityProfileId\":$PROFILE_ID,\"tags\":[$TAG_ID],\"applyTags\":\"add\"}" \
  "$RADARR/api/v3/movie/editor" >/dev/null

# optional: search so Radarr can grab a release that fits the new profile
curl -fsS -X POST -H "X-Api-Key: $RADARR_API_KEY" -H "Content-Type: application/json" \
  -d "{\"name\":\"MoviesSearch\",\"movieIds\":$movie_ids}" \
  "$RADARR/api/v3/command" >/dev/null

echo "Re-profiled $(jq 'length' <<<"$movie_ids") movie(s)"
```

### Radarr: Move Old Movies to Another Root Folder

Moves movies nobody has watched in two years to a slower disk. Radarr moves the files.

Rule (movie version scope, match only):

- **Never watched** is true
- **Days since added** greater than `730`
- **Arr tags** does not contain `archived`

Use the previous script and change only the editor request:

```bash
ROOT_FOLDER="/mnt/archive/movies"   # must already be a root folder in Radarr
TAG_ID=5                            # the "archived" tag

curl -fsS -X PUT -H "X-Api-Key: $RADARR_API_KEY" -H "Content-Type: application/json" \
  -d "{\"movieIds\":$movie_ids,\"rootFolderPath\":\"$ROOT_FOLDER\",\"moveFiles\":true,\"tags\":[$TAG_ID],\"applyTags\":\"add\"}" \
  "$RADARR/api/v3/movie/editor" >/dev/null
```

Your media server must have a library that covers the new folder, or the movies will drop out of it. Paths in Reclaimerr update after the media server rescans and the next **Sync Media** run.

### Sonarr: Tag and Re-profile Whole Series

Same pattern as the Radarr script, for a whole-series rule. Change the filter and the editor request:

```bash
series_ids=$(jq -c --argjson cfg "$SONARR_CONFIG_ID" \
  '[.items[].arr[] | select(.service == "sonarr" and .service_config_id == $cfg) | .arr_id] | unique' \
  <<<"$matches")

curl -fsS -X PUT -H "X-Api-Key: $SONARR_API_KEY" -H "Content-Type: application/json" \
  -d "{\"seriesIds\":$series_ids,\"qualityProfileId\":$PROFILE_ID,\"tags\":[$TAG_ID],\"applyTags\":\"add\"}" \
  "$SONARR/api/v3/series/editor" >/dev/null
```

`/api/v3/series/editor` also accepts `rootFolderPath` with `moveFiles`, and `monitored`.

### Sonarr: Unmonitor Fully Watched Seasons

Stops Sonarr from searching for older seasons that have been watched to the end. **Season fully watched** reads the media server's watch state, which combines every viewer; use **Fully watched by users** instead when specific people must have finished. This needs per-season changes, so it is written in Python (`pip install requests`).

Rule (season scope, match only):

- **Season fully watched** is true
- **Is latest season** is false
- **Arr monitored** is true

```python
#!/usr/bin/env python3
import os
import sys
from collections import defaultdict

import requests

RECLAIMERR = os.environ["RECLAIMERR"]
RECLAIMERR_TOKEN = os.environ["RECLAIMERR_TOKEN"]
SONARR = os.environ["SONARR"]
SONARR_API_KEY = os.environ["SONARR_API_KEY"]
RULE_ID = 15
SONARR_CONFIG_ID = 2  # service_config_id of this Sonarr in Reclaimerr
DRY_RUN = os.environ.get("DRY_RUN") == "1"

response = requests.get(
    f"{RECLAIMERR}/api/v1/rules/{RULE_ID}/matches",
    headers={"Authorization": f"Bearer {RECLAIMERR_TOKEN}"},
    timeout=600,
)
response.raise_for_status()
matches = response.json()
if not matches["complete"]:
    sys.exit("Incomplete data, skipping: " + "; ".join(matches["warnings"]))

# Sonarr series ID -> season numbers to unmonitor
seasons_by_series = defaultdict(set)
for item in matches["items"]:
    for ref in item["arr"]:
        if ref["service"] == "sonarr" and ref["service_config_id"] == SONARR_CONFIG_ID:
            seasons_by_series[ref["arr_id"]].add(item["season_number"])

sonarr = requests.Session()
sonarr.headers["X-Api-Key"] = SONARR_API_KEY
for series_id, season_numbers in seasons_by_series.items():
    lookup = sonarr.get(f"{SONARR}/api/v3/series/{series_id}", timeout=30)
    lookup.raise_for_status()
    series = lookup.json()
    if DRY_RUN:
        print(f"Would unmonitor {series['title']} seasons {sorted(season_numbers)}")
        continue
    for season in series["seasons"]:
        if season["seasonNumber"] in season_numbers:
            season["monitored"] = False
    sonarr.put(
        f"{SONARR}/api/v3/series/{series_id}", json=series, timeout=30
    ).raise_for_status()
    print(f"Unmonitored {series['title']} seasons {sorted(season_numbers)}")
```

For an episode rule, get the episode IDs from `GET /api/v3/episode?seriesId={id}` by matching `seasonNumber` and `episodeNumber`, then send them to `PUT /api/v3/episode/monitor` with `{"episodeIds": [...], "monitored": false}`.

## Run It on a Schedule

Run the script after Reclaimerr's syncs so it sees fresh data. A daily cron entry:

```text
30 4 * * * DRY_RUN=0 /opt/scripts/reprofile-4k.sh >> /var/log/reprofile-4k.log 2>&1
```

Each request evaluates the rule from scratch, which can take a while on a large library. Reclaimerr handles these requests one at a time, so run scripts minutes apart rather than in a tight loop. Allow a long HTTP timeout.

## Troubleshooting

| Problem | Cause |
| --- | --- |
| `401` | The token is missing, wrong, revoked, or expired |
| `403` | The token lacks `rules:read` |
| `404` | No rule has that ID |
| `409` | The rule is disabled, or it is a cleanup or protection rule rather than match only |
| `complete` is `false` | A provider the rule depends on, such as Seerr, playback history, or Sonarr, had no data. Read `warnings`. |
| A match has an empty `arr` list | The title isn't in any Radarr or Sonarr instance Reclaimerr knows about, or **Sync Media** hasn't run since it was added |
| The same items match on every run | The rule has no guard condition. See [Make Reruns Harmless](#make-reruns-harmless). |

## Related Pages

- [Rules](rules.md)
- [External API](../reference/api.md)
- [Rule Fields](../reference/rule-fields.md)
