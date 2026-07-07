# Rules

Rules determine which media becomes a reclaim candidate or receives an
automated protection. A rule has:

- a target scope
- one or more conditions
- nested `AND` or `OR` groups
- an outcome: cleanup candidate or automated protection
- candidate action settings when the outcome is cleanup

Use rule preview before saving or running a cleanup scan. Preview shows the
items that match and the actual values used for each matching condition.

## Rule Outcomes

| Outcome              | Behavior                                                              |
| -------------------- | --------------------------------------------------------------------- |
| Cleanup candidate    | Matching media enters the normal review and deletion workflow         |
| Automated protection | Matching media is protected from cleanup without creating a candidate |

Automated protections are reconciled on each cleanup scan. They are added when
a rule starts matching and removed when that same rule no longer matches, is
disabled, or is deleted. Manual protections are not changed by this process.
Library Scope applies to both cleanup candidate and automated protection
outcomes.

Each protection rule creates its own managed protection entry for a matching
item. These entries are read-only on the Protected page because changing the
rule is the source of truth. If a protection rule and a candidate rule match
the same item, protection always takes precedence.

Cleanup-candidate rules can optionally override the automatic deletion delay.
Leave the override empty to inherit the global movie or TV delay. Values from
`0` through `3650` days are supported, with `0` meaning immediately eligible.
When multiple cleanup-candidate rules match the same item, Reclaimerr uses the
longest applicable delay so a shorter rule cannot reduce another rule's review
period.

## Target Scopes

Fields are limited to scopes where Reclaimerr has meaningful data.

| Scope         | Evaluated item            | Examples                                                |
| ------------- | ------------------------- | ------------------------------------------------------- |
| Movie version | One physical movie file   | Container, bitrate, codec, subtitles                    |
| Series        | The complete local series | Status, year, season counts                             |
| Season        | One local season          | Season number, episode count, inherited series metadata |
| Episode       | One local episode         | Episode number, air date, inherited series metadata     |

A movie-version rule evaluates each physical version independently. If a movie
has multiple files, more than one version can become a candidate.

## Condition Groups

An `AND` group matches only when every child condition matches. An `OR` group
matches when at least one child condition or child group matches.

For example, this identifies old, unwatched movies that are either large or
have multiple versions:

```text
AND
  Never watched is true
  Days since added >= 180
  OR
    Size > 21474836480
    Movie version count > 1
```

`21474836480` is 20 GiB expressed in bytes, which is the unit expected by the
size field.

## Operators

### List Operators

| Internal operator  | UI label           | Meaning                                    |
| ------------------ | ------------------ | ------------------------------------------ |
| `contains_any`     | matches any        | At least one supplied value matches        |
| `not_contains_any` | matches none       | None of the supplied values match          |
| `contains_all`     | matches all        | Every supplied value matches               |
| `not_contains_all` | does not match all | At least one supplied value does not match |

Text and list comparisons are case-insensitive unless a field documents
additional normalization.

### Substring Operators (Arr tags)

The Arr tags field matches whole tag names with the list operators above. It
also supports substring matching, which matches a tag by a fragment of its
name. Each operator takes comma-separated terms with the same any/none
behavior as `matches any` and `matches none`.

| Internal operator        | UI label         | Meaning                            |
| ------------------------ | ---------------- | ---------------------------------- |
| `contains_substring`     | contains         | Some tag contains one of the terms |
| `not_contains_substring` | does not contain | No tag contains any of the terms   |

For example, `contains chart` matches a tag such as `weekly-chart-2024`, and
`contains chart, -best` matches a tag containing either fragment. To require
several fragments at once, combine `contains` conditions with an AND group. A
blank term matches nothing.

### Missing Values

`exists` matches populated metadata. `does not exist` matches missing or empty
metadata.

Missing metadata does not automatically prove a negative condition. Language
and origin-country rules therefore fail closed: if the item's value is unknown,
`matches none` and `does not match all` do not match it. Use a separate `does
not exist` condition when you specifically want to identify missing metadata.

## Field Reference

The rule editor only displays fields valid for the selected scope. The
following fields have behavior or units that are important when constructing a
rule.

### General Media Fields

| Field                            | Scope         | Value                                                    |
| -------------------------------- | ------------- | -------------------------------------------------------- |
| Year                             | All scopes    | Movie year or the parent series year                     |
| Size                             | All scopes    | Bytes for the evaluated file, series, season, or episode |
| Duration                         | Movie version | Media-server duration in milliseconds                    |
| Container                        | Movie version | File container such as `mkv` or `mp4`                    |
| Path / Filename                  | All scopes    | Local media-server path information                      |
| Days since added                 | All scopes    | Age of the existing media-server added date              |
| Days since latest Arr file added | All scopes    | Age of Radarr/Sonarr's latest file-import date           |

Arr file-added dates are populated during Radarr and Sonarr syncs. They remain
empty when an item cannot be matched or an Arr service is not configured; the
existing media-server added date is not replaced or backfilled.

### TMDB Metadata

| Field                       | Scope                   | Value                                              |
| --------------------------- | ----------------------- | -------------------------------------------------- |
| Original language           | All scopes              | Canonical ISO 639-3 language code                  |
| Origin country              | All scopes              | Case-insensitive country code such as `US` or `JP` |
| Runtime                     | Movie version           | TMDB movie runtime in minutes                      |
| Genres                      | All scopes              | TMDB genre names                                   |
| Rating / Votes / Popularity | All scopes              | Current stored TMDB metadata                       |
| Release date                | Movie version           | Movie release date                                 |
| First / last air date       | Series, season, episode | Dates inherited from the parent series             |

Original-language values are normalized before comparison. For example, `en`,
`eng`, and `English` all compare as `eng`. The picker displays languages found
in the local database, but manual entry remains available.

Origin-country comparisons are case-insensitive. The country picker displays
codes currently found in local TMDB metadata.

### External Ratings

External rating fields are available to all scopes. Movie-version rules use the
parent movie's cached values. Series, season, and episode rules use the parent
series values.

| Field                              | Source                 | Value            |
| ---------------------------------- | ---------------------- | ---------------- |
| Rotten Tomatoes Tomatometer        | MDBList, fallback OMDb | Percent, `0-100` |
| Rotten Tomatoes Tomatometer votes  | MDBList                | Count            |
| Rotten Tomatoes Popcornmeter       | MDBList                | Percent, `0-100` |
| Rotten Tomatoes Popcornmeter votes | MDBList                | Count            |
| Metacritic metascore               | MDBList, fallback OMDb | Score, `0-100`   |
| Metacritic critic count            | MDBList                | Count            |
| Metacritic user score              | MDBList                | Score, `0-100`   |
| Metacritic user votes              | MDBList                | Count            |
| Trakt rating                       | MDBList                | Percent, `0-100` |
| Trakt votes                        | MDBList                | Count            |
| Letterboxd score                   | MDBList                | Percent, `0-100` |
| Letterboxd votes                   | MDBList                | Count            |

MDBList is preferred because it provides structured `ratings[]` entries for
Rotten Tomatoes, Metacritic, Trakt, and Letterboxd plus vote counts. OMDb is
used as a fallback for Tomatometer and Metacritic when an IMDb ID is available.
Direct Rotten Tomatoes and Metacritic scraping is intentionally not used.

Ratings are refreshed by the provider-specific `Refresh MDBList Ratings` and
`Refresh OMDb Ratings` tasks. They keep independent schedules and caches.
MDBList values remain authoritative, while OMDb fills missing Tomatometer and
Metacritic values. If a provider has not been configured, the media has no
matching provider ID, or the provider does not return a rating, that field is
missing. Numeric comparisons do not match missing ratings; use `does not exist`
when you specifically want to find media without a cached rating.

The Metadata Providers settings page shows per-refresh request usage and cached
movie/series coverage for MDBList and OMDb. Provider rate-limit headers are
tracked internally only to stop refresh work when a provider reports that its
quota is exhausted.

MDBList requests are paced during external-rating refreshes. Standard mode uses
a 1 second minimum delay between MDBList requests; MDBList supporter mode uses a
0.2 second delay.

### Movie-Version Metadata

| Field                | Unit or value                                    |
| -------------------- | ------------------------------------------------ |
| Video bitrate        | Kilobits per second (`kbps`)                     |
| Audio bitrate        | Kilobits per second (`kbps`)                     |
| Video bit depth      | Bits, commonly `8`, `10`, or `12`                |
| Subtitle track count | Number of subtitle streams                       |
| Has forced subtitles | Boolean                                          |
| Movie version count  | Number of physical versions stored for the movie |

Plex bitrate values are already stored as `kbps`. Jellyfin and Emby commonly
report bits per second, so Reclaimerr converts those values to `kbps` during
rule evaluation. This provides the same rule units across media servers without
rewriting stored metadata.

`Movie version count` is inherited by every version of the movie. A condition
such as `Movie version count > 1` therefore selects every version of each
multi-version movie. Combine it with a distinguishing condition such as
quality, codec, resolution, size, bitrate, container, or path when you intend
to remove only one version.

### Series Season Counts

| Field                | Meaning                                              |
| -------------------- | ---------------------------------------------------- |
| TMDB season count    | Number of seasons reported by TMDB                   |
| Library season count | Number of locally stored seasons, excluding season 0 |

Season 0 is normally used for specials and is intentionally excluded from the
library season count. Both count fields are available to series, season, and
episode rules and are inherited from the parent series.

These values may differ when the local library contains only part of a series,
TMDB metadata has changed, or specials are present.

### Seerr Request Dates

Seerr request dates are available to movie-version, series, season, and episode
rules:

| Field                                  | Meaning                                      |
| -------------------------------------- | -------------------------------------------- |
| Seerr latest active request            | Newest pending or approved request timestamp |
| Days since latest active Seerr request | Whole days since that request                |

Declined and failed requests are excluded. Series rules use the latest request
for the series. Season and episode rules use the request for their specific
season, so requesting a later season does not reset the request age of an
earlier season. Older Seerr responses that do not identify requested seasons
fall back to the series request date. If Seerr is unavailable, these values are
unknown and cannot create candidates.

### Seerr Requester Watch State

`Seerr requester has watched` compares each requester's playback with the time
they requested the movie or TV season. Playback at or before the request does
not count. The meaning depends on the rule target:

| Scope         | `is true` means                                                                 |
| ------------- | ------------------------------------------------------------------------------- |
| Movie version | A requester watched the movie after requesting it                               |
| Episode       | A requester watched that episode after requesting its season                    |
| Season        | One requester watched every local episode in that requested season              |
| Series        | One requester watched every regular local episode in the seasons they requested |

Season 0 specials are excluded from series completion. Progress from different
requesters is never combined to complete a season or series. Seasons that were
not requested do not inherit another season's requested or watched state.

Reclaimerr first matches Seerr users automatically using usernames, display
names, and email addresses from Seerr's user directory. Explicit requester
watch-user mappings are additive fallbacks for users whose identities differ
between Seerr and the playback provider. Username comparisons are
case-insensitive. Tautulli identities are treated as Plex identities when
applying provider-scoped mappings.

Requester watch state combines completed per-user playback snapshots from
Plex, Jellyfin, and Emby with Tautulli events whose provider-native watched
status is complete. Each provider's configured watched threshold remains the
source of truth. Jellyfin and Emby Playback Reporting events describe activity
but do not expose a reliable completion signal, so they do not independently
satisfy this field. They remain available to the general `playback.*` fields.
When the same completed play is available from multiple sources, Reclaimerr
keeps the latest qualifying timestamp.

After configuring Seerr or changing identity mappings, run `Sync Media` before
previewing the rule. Plex can provide current completed-watch state directly;
durable completed Plex history requires Tautulli.

### Sonarr Rule Data

`Sonarr series status` exposes Sonarr's canonical series status independently
from the TMDB-backed `Series status` field. It is available to series, season,
and episode rules with the values `continuing`, `ended`, `upcoming`, and
`deleted`.

Status is loaded from Sonarr's bulk series response and does not require
episode requests. When a series maps to multiple Sonarr instances, every
mapped and reachable instance must report the same status. Missing or
conflicting values are unknown and fail closed.

The latest-season fields are available only to whole-series rules:

| Field                              | Meaning                                                         |
| ---------------------------------- | --------------------------------------------------------------- |
| Latest season has unaired episodes | The latest regular Sonarr season has an episode airing later    |
| Latest season has finale           | The latest regular season has a `season` or `series` finale tag |

Reclaimerr ignores season 0 and checks only the highest-numbered regular
season. This keeps scans efficient while covering upcoming seasons and split
cours. Episode monitoring status is not considered.

Sonarr's series statistics may provide a future `nextAiring` value. Reclaimerr
uses that value to prove that an unaired episode exists without requesting the
season's episodes. A missing `nextAiring` value cannot prove that no future
episode exists, so Reclaimerr requests only the latest season's episodes when
the rule result still depends on Sonarr data.

Episode-state data is loaded only when an enabled rule uses one of these
fields. Requests are cached for the current preview or cleanup scan and are
limited to eight concurrent episode requests per Sonarr instance.

Unavailable, empty, or malformed Sonarr data is treated as unknown. Unknown
values match neither `is true`, `is false`, `exists`, nor `does not exist`.
They cannot create a cleanup candidate. Existing automated protections are
preserved for the affected rule and series until Sonarr can be evaluated
again.

When a series is mapped to multiple Sonarr instances, `true` wins if any
instance proves it. `false` is returned only when every mapped instance
successfully reports false. Otherwise the value remains unknown.

Typical protection rules are:

```text
Latest season has unaired episodes is true
```

```text
Latest season has finale is false
```

The finale field depends on Sonarr's metadata and may remain false while a
season is incomplete or its finale metadata has not been updated. Combine it
with status, age, watch-history, or library conditions and inspect the preview
before enabling the rule.

### Durable Playback History

Playback history fields are available to movie-version, series, season, and
episode rules. Reclaimerr imports compact events from the Jellyfin/Emby
Playback Reporting plugin and Tautulli, then evaluates provider-neutral fields:

| Field                        | Meaning                                                |
| ---------------------------- | ------------------------------------------------------ |
| Imported playback activity   | At least one qualifying imported playback event        |
| Playback plays               | Number of qualifying playback events                   |
| Playback duration            | Total qualifying playback minutes                      |
| Longest playback             | Longest qualifying playback in minutes                 |
| Playback user count          | Number of distinct source users with qualifying events |
| Playback users               | Resolved usernames with qualifying events              |
| Last playback activity       | Most recent qualifying event timestamp                 |
| Days since playback activity | Whole days since the most recent event                 |

Movie events shorter than 15 seconds and episode events shorter than 7 seconds
are ignored. These thresholds prevent brief scrubs from counting as activity.

Events are retained locally until their source service configuration is
deleted. They are mapped by exact media-server IDs and stable TMDB,
season-number, and episode-number identities, so imported history can survive
provider retention cleanup and media deletion/re-addition. Title-only matching
is never used.

The existing `watch.*` fields continue to describe the current library copy.
The `playback.*` fields describe durable imported history and may therefore
include activity from before the current copy was added.

`Imported playback activity` is either true or false when a configured
provider can observe the media target. Targets outside provider coverage are
unknown and match neither value. Marking an item watched without playing it
updates the current `watch.*` state but does not create an imported playback
event.

Playback data is loaded only when an enabled rule uses a `playback.*` field.
Tautulli history is fetched in one ungrouped paginated pass. Playback Reporting
and Tautulli imports use an overlap window and event keys to avoid duplicate
events during incremental refreshes.

Playback-user rules match usernames case-insensitively and can require any,
none, or all selected users. Jellyfin and Emby resolve names from their native
user APIs; Tautulli resolves only the Plex history it supplies. If any user on
an item cannot be resolved, username conditions remain unavailable for that
item while the other playback metrics remain usable.

Plex durable playback history, including playback-user rules, requires
Tautulli. Reclaimerr does not currently import durable playback events directly
from Plex.

If no applicable provider is configured, a provider request fails, or an item
cannot be observed through an available provider, playback values are unknown.
Unknown values cannot create cleanup candidates. Existing automated
protections are preserved until the affected playback rule can be evaluated
again.

## Validation and Editing

- Operator choices are limited to operators supported by the selected field.
- Field choices are limited to the selected target scope.
- Changing a field resets an incompatible operator to the field's default.
- Existing saved rules are not rewritten until they are edited and saved.
- Backend validation rejects unsupported field, operator, or scope
  combinations.
- Rule preview uses the same evaluation logic as cleanup candidate scans.

## Recommended Workflow

1. Choose whether the rule creates cleanup candidates or automated protections.
2. Select the narrowest target scope that represents the intended item.
3. Add positive conditions that identify the media.
4. Add quality, age, watch-history, or metadata conditions to reduce broad
   matches.
5. Preview the rule and inspect the displayed actual values.
6. Save the rule only after the preview contains the intended files.
7. Run a cleanup scan to reconcile candidates and automated protections.

## Related Pages

- [How It Works](how-it-works.md)
- [Tasks](tasks.md)
- [API Reference](../reference/api.md)
