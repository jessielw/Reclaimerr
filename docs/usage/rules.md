# Rules

Rules determine which media becomes a reclaim candidate or receives an automated protection. A rule has:

- a short name and optional plain-text description for identifying it throughout the app
- a target scope
- one or more conditions
- nested `AND` or `OR` groups
- an outcome: cleanup candidate or automated protection
- candidate action settings when the outcome is cleanup

Use rule preview before saving or running a cleanup scan. Preview shows the items that match and the actual values used for each matching condition.

## Rule Outcomes

| Outcome | Behavior |
| --- | --- |
| Cleanup candidate | Matching media enters the normal review and deletion workflow |
| Automated protection | Matching media is protected from cleanup without creating a candidate |

Automated protections are reconciled on each cleanup scan. They are added when a rule starts matching and removed when that same rule no longer matches, is disabled, or is deleted. Manual protections are not changed by this process. Library Scope applies to both cleanup candidate and automated protection outcomes.

Library Scope lists the libraries of the configured main media server, since only the main server contributes library contents. With more than one media server configured each library is listed under that server's name, so two servers' identically-named libraries stay distinguishable; with one server the heading is left off. See [Multi-Server Setup](../getting-started/configuration.md#multi-server-setup).

Each protection rule creates its own managed protection entry for a matching item. These entries are read-only on the Protected page because changing the rule is the source of truth. If a protection rule and a candidate rule match the same item, protection always takes precedence.

Cleanup-candidate rules can optionally enable automatic deletion. Rules that do not enable it still create candidates and can populate Leaving Soon collections, but the scheduled delete task skips them.

Cleanup-candidate rules can also enable Move Instead of Delete. Delete actions for candidates matched by those rules move media to the configured destination folder instead of deleting the file. Destination folders are configured in General Settings. If multiple matched cleanup rules disagree, move wins.

Cleanup-candidate rules can target one or more Radarr or Sonarr instances. Reclaimerr applies the rule's managed tag to every selected instance where the item exists and limits ARR deletion or unmonitor actions to those selections. For movie versions, the synchronized Radarr movie folder must match the media-server file path before an explicitly selected instance is used. Configure instance-scoped Path Mappings in General Settings when the services report different container path prefixes. Leaving every instance unselected preserves automatic path-based routing across all matching active instances.

When automatic deletion is enabled for a rule, the rule can also override the review period. Leave the override empty to inherit the default movie or TV delay. Values from `0` through `3650` days are supported, with `0` meaning immediately eligible. When multiple auto-delete-enabled cleanup rules match the same item, Reclaimerr uses the longest applicable delay so a shorter rule cannot reduce another rule's review period.

## Target Scopes

Fields are limited to scopes where Reclaimerr has meaningful data.

| Scope | Evaluated item | Examples |
| --- | --- | --- |
| Movie version | One physical movie file | Container, bitrate, codec, subtitles |
| Series | The complete local series | Status, year, season counts |
| Season | One local season | Season number, episode count, inherited series metadata |
| Episode | One local episode | Episode number, air date, inherited series metadata |

A movie-version rule evaluates each physical version independently. If a movie has multiple files, more than one version can become a candidate.

## Condition Groups

An `AND` group matches only when every child condition matches. An `OR` group matches when at least one child condition or child group matches.

For example, this identifies old, unwatched movies that are either large or have multiple versions:

```text
AND
  Never watched is true
  Days since added >= 180
  OR
    Size > 21474836480
    Movie version count > 1
```

`21474836480` is 20 GiB expressed in bytes, which is the unit expected by the size field.

## Operators

### List Operators

| Internal operator | UI label | Meaning |
| --- | --- | --- |
| `contains_any` | matches any | At least one supplied value matches |
| `not_contains_any` | matches none | None of the supplied values match |
| `contains_all` | matches all | Every supplied value matches |
| `not_contains_all` | does not match all | At least one supplied value does not match |

Text and list comparisons are case-insensitive unless a field documents additional normalization.

### Substring Operators (Arr tags)

The Arr tags field matches whole tag names with the list operators above. It also supports substring matching, which matches a tag by a fragment of its name. Each operator takes comma-separated terms with the same any/none behavior as `matches any` and `matches none`.

| Internal operator | UI label | Meaning |
| --- | --- | --- |
| `contains_substring` | contains | Some tag contains one of the terms |
| `not_contains_substring` | does not contain | No tag contains any of the terms |

For example, `contains chart` matches a tag such as `weekly-chart-2024`, and `contains chart, -best` matches a tag containing either fragment. To require several fragments at once, combine `contains` conditions with an AND group. A blank term matches nothing.

### Regex Operators (Arr tags)

The Arr tags field also matches tags with regular expressions. Each operator takes one or more patterns; a pattern is applied with `re.search` and is case-insensitive, so anchor with `^` and `$` to match a whole tag. Patterns within one operator combine with the same any/none behavior as the list operators.

| Internal operator | UI label | Meaning |
| --- | --- | --- |
| `matches_any_regex` | matches regex | Some tag matches one of the patterns |
| `not_matches_any_regex` | does not match regex | No tag matches any of the patterns |

`matches_any_regex` is also available on the path fields. A condition with no valid pattern matches nothing, so `not_matches_any_regex` never matches an item on an empty or invalid pattern.

For example, given tags that pair a base name with a `-stale` variant (such as `tag-1` and `tag-1-stale`), an AND group of `matches_any_regex` `tag-.*-stale$` and `not_matches_any_regex` `^tag-.*(?<!-stale)$` selects items that carry a `-stale` variant but no active base tag. An item still active under any tag keeps a value matching the second pattern, so it is excluded.

### Missing Values

`exists` matches populated metadata. `does not exist` matches missing or empty metadata.

Missing metadata does not automatically prove a negative condition. Language and origin-country rules therefore fail closed: if the item's value is unknown, `matches none` and `does not match all` do not match it. Use a separate `does not exist` condition when you specifically want to identify missing metadata.

Provider-specific media-server genres also fail closed when their provider is not the configured main media server. For example, `Plex genres matches none Drama` does not match an item whose main server is Jellyfin, because Plex metadata is unavailable rather than empty.

## Field Reference

The rule editor only displays fields valid for the selected scope. The following fields have behavior or units that are important when constructing a rule.

### General Media Fields

| Field | Scope | Value |
| --- | --- | --- |
| Title | All scopes | Movie title or parent series title |
| Year | All scopes | Movie year or the parent series year |
| Size | All scopes | Bytes for the evaluated file, series, season, or episode |
| Duration | Movie version | Media-server duration in milliseconds |
| Container | Movie version | File container such as `mkv` or `mp4` |
| Path / Filename | All scopes | Local media-server path information |
| Days since added | All scopes | Age of the existing media-server added date |
| Days since latest Arr file added | All scopes | Age of Radarr/Sonarr's latest file-import date |
| Media server user rating | All scopes | User rating reported by Plex, Jellyfin, or Emby |

Size and disk-free conditions use an amount and unit selector in the editor. Rules continue to store integer byte values, using 1024-based KB, MB, GB, and TB conversions for compatibility with existing rules.

Arr file-added dates are populated during Radarr and Sonarr syncs. They remain empty when an item cannot be matched or an Arr service is not configured; the existing media-server added date is not replaced or backfilled.

Media-server user ratings are stored during media sync when the provider reports a user-specific rating for the item. Missing ratings do not match numeric comparisons; use `does not exist` to find unrated media.

### Media-Server Genres

| Field           | Scope      | Value                            |
| --------------- | ---------- | -------------------------------- |
| Plex genres     | All scopes | Genre tags reported by Plex      |
| Jellyfin genres | All scopes | Genre names reported by Jellyfin |
| Emby genres     | All scopes | Genre names reported by Emby     |

Media-server genres are separate from TMDB genres. Reclaimerr does not merge names between providers, even when the same name appears in both sources. This makes it possible to combine them explicitly with `AND` or `OR` groups and keeps locally edited or provider-specific genres attributable to their source.

Only genres from the configured main media server are stored. Linked media servers continue to contribute supplemental watch data, not library metadata. A condition for a different provider is unavailable and does not match, including with negative list operators. If the main provider reports an item but gives it no genres, `does not exist` can match that empty provider value.

Run a full media sync after upgrading to populate provider genres for existing media. Series-level media-server genres are inherited by series, season, and episode rule scopes; movie genres are evaluated per movie version.

### Arr Identifiers

| Field | Scope | Value |
| --- | --- | --- |
| Radarr movie IDs | Movie version | One or more Radarr IDs for the movie |
| Sonarr series IDs | Series, season, episode | One or more Sonarr IDs for the series |

Reclaimerr can map one local item to multiple Radarr or Sonarr instances, so Arr ID fields are multi-value text fields. Use `matches any` for a precise rule against one known Arr ID, or `matches all` when an item must be present in several configured Arr instances.

### Favorites and Watchlists

Favorite/watchlist rule fields use the same snapshot that powers favorites protection. Jellyfin and Emby favorites are included, and Plex watchlists are included for linked Plex users.

| Field | Scope | Value |
| --- | --- | --- |
| Favorited or watchlisted | All scopes | Boolean, true when any known user has the item |
| Favorite/watchlist users | All scopes | Usernames from the favorite/watchlist snapshot |
| Favorite/watchlist user count | All scopes | Number of unique users with the item favorited |

For season and episode rules, favorite/watchlist state is inherited from the parent series TMDB ID.

### Retention Position Fields

| Field | Scope | Value |
| --- | --- | --- |
| Season position by air date | Season | Newest local regular season in the series is `1` |
| Episode position by air date | Episode | Newest local regular episode in the series is `1` |

Season 0 specials are excluded. Items without an air date or Arr file-added date have unknown rank and do not match numeric comparisons. These fields are intended for rules such as "keep the newest 2 seasons" or "keep the newest 10 episodes" by matching positions greater than the number you want to keep.

### Collection Sibling Activity

Movie-version rules can check whether another movie in the same media-server collection has been watched recently.

| Field | Scope | Value |
| --- | --- | --- |
| Collection sibling last watched | Movie version | Latest watch timestamp from another movie sibling |
| Days since collection sibling watched | Movie version | Whole days since that sibling watch timestamp |

The current movie is excluded from the sibling calculation. Reclaimerr-managed Leaving Soon collections are ignored so temporary cleanup collections do not affect rules.

### TMDB Metadata

| Field | Scope | Value |
| --- | --- | --- |
| Original language | All scopes | Canonical ISO 639-3 language code |
| Origin country | All scopes | Case-insensitive country code such as `US` or `JP` |
| Runtime | Movie version | TMDB movie runtime in minutes |
| Genres | All scopes | TMDB genre names; never merged with media-server genres |
| Rating / Votes / Popularity | All scopes | TMDB rating uses the raw 0-10 `vote_average` scale; votes and popularity use current stored TMDB metadata |
| Release date | Movie version | Movie release date |
| First / last air date | Series, season, episode | Dates inherited from the parent series |

Original-language values are normalized before comparison. For example, `en`, `eng`, and `English` all compare as `eng`. The picker displays languages found in the local database, but manual entry remains available.

Origin-country comparisons are case-insensitive. The country picker displays codes currently found in local TMDB metadata.

TMDB rating comparisons use the raw `vote_average` value from TMDB on a 0-10 scale. That is different from percentage-style ratings elsewhere in the app.

TMDB has no rating for a title nobody has voted on. Reclaimerr stores that as an empty value rather than as a score of zero, so a comparison such as `<` never matches an unrated title. Use `does not exist` on the TMDB rating field when you specifically want to find those titles.

### External Ratings

External rating fields are available to all scopes. Movie-version rules use the parent movie's cached values. Series, season, and episode rules use the parent series values.

| Field | Source | Value |
| --- | --- | --- |
| Rotten Tomatoes Tomatometer | MDBList, fallback OMDb | Percent, `0-100` |
| Rotten Tomatoes Tomatometer votes | MDBList | Count |
| Rotten Tomatoes Popcornmeter | MDBList | Percent, `0-100` |
| Rotten Tomatoes Popcornmeter votes | MDBList | Count |
| Metacritic metascore | MDBList, fallback OMDb | Score, `0-100` |
| Metacritic critic count | MDBList | Count |
| Metacritic user score | MDBList | Score, `0-100` |
| Metacritic user votes | MDBList | Count |
| Trakt rating | MDBList | Percent, `0-100` |
| Trakt votes | MDBList | Count |
| Letterboxd score | MDBList | Percent, `0-100` |
| Letterboxd votes | MDBList | Count |

MDBList is preferred because it provides structured `ratings[]` entries for Rotten Tomatoes, Metacritic, Trakt, and Letterboxd plus vote counts. OMDb is used as a fallback for Tomatometer and Metacritic when an IMDb ID is available. Direct Rotten Tomatoes and Metacritic scraping is intentionally not used.

Metacritic user score and Letterboxd score are stored and matched on a 0-100 scale, but the providers publish them differently: Metacritic shows its user score as 0-10 on its own site, and Letterboxd shows its score as 0-5. Build rules against the stored 0-100 value, not the number shown on the provider's site.

Ratings are refreshed by the provider-specific `Refresh MDBList Ratings` and `Refresh OMDb Ratings` tasks. They keep independent schedules and caches. MDBList values remain authoritative, while OMDb fills missing Tomatometer and Metacritic values. If a provider has not been configured, the media has no matching provider ID, or the provider does not return a rating, that field is missing. Numeric comparisons do not match missing ratings; use `does not exist` when you specifically want to find media without a cached rating.

The Metadata Providers settings page shows per-refresh request usage and cached movie/series coverage for MDBList and OMDb. Provider rate-limit headers are tracked internally only to stop refresh work when a provider reports that its quota is exhausted.

MDBList requests are paced during external-rating refreshes. Standard mode uses a 1 second minimum delay between MDBList requests; MDBList supporter mode uses a 0.2 second delay.

### Movie-Version Metadata

| Field                | Unit or value                                    |
| -------------------- | ------------------------------------------------ |
| Video bitrate        | Kilobits per second (`kbps`)                     |
| Audio bitrate        | Kilobits per second (`kbps`)                     |
| Video bit depth      | Bits, commonly `8`, `10`, or `12`                |
| Subtitle track count | Number of subtitle streams                       |
| Has forced subtitles | Boolean                                          |
| Movie version count  | Number of physical versions stored for the movie |

Plex bitrate values are already stored as `kbps`. Jellyfin and Emby commonly report bits per second, so Reclaimerr converts those values to `kbps` during rule evaluation. This provides the same rule units across media servers without rewriting stored metadata.

`Movie version count` is inherited by every version of the movie. A condition such as `Movie version count > 1` therefore selects every version of each multi-version movie. Combine it with a distinguishing condition such as quality, codec, resolution, size, bitrate, container, or path when you intend to remove only one version.

### Series Season Counts

| Field                | Meaning                                              |
| -------------------- | ---------------------------------------------------- |
| TMDB season count    | Number of seasons reported by TMDB                   |
| Library season count | Number of locally stored seasons, excluding season 0 |

Season 0 is normally used for specials and is intentionally excluded from the library season count. Both count fields are available to series, season, and episode rules and are inherited from the parent series.

These values may differ when the local library contains only part of a series, TMDB metadata has changed, or specials are present.

### Seerr Request Dates

Seerr request dates are available to movie-version, series, season, and episode rules:

| Field | Meaning |
| --- | --- |
| Seerr latest active request | Newest pending or approved request timestamp |
| Days since latest active Seerr request | Whole days since that request |

Declined and failed requests are excluded. Series rules use the latest request for the series. Season and episode rules use the request for their specific season, so requesting a later season does not reset the request age of an earlier season. Older Seerr responses that do not identify requested seasons fall back to the series request date. If Seerr is unavailable, these values are unknown and cannot create candidates.

### Several Seerr Instances

Every configured Seerr answers the same question and the answers are combined. `Seerr requested` is true if any instance holds a request, and `Seerr latest active request` is the newest live request across all of them -- so `Days since latest active Seerr request` will not pass a threshold while any instance still holds a recent request.

Requesters are named per instance as `instanceId:userId` -- for example `7:3` -- because a Seerr user ID only identifies a person inside the Seerr that issued it, the same reason provider IDs never bridge across playback providers. The requester picker writes these for you, and groups its list by instance. The same person with an account on two Seerrs is two requesters, and neither one's watch progress or request dates count towards the other.

Rules exported before this release name requesters by a bare user ID. Importing one attaches your Seerr to it automatically when a single Seerr is configured. With none or several configured the rule is refused rather than guessed at, and the import error says why -- re-pick its requesters in the rule editor. A rule naming an instance ID this install does not have still imports, with a warning: its requester conditions match nobody until they are re-picked.

**If any configured Seerr cannot be read, every Seerr-dependent rule is skipped for that run** and an admin notice names the instance. Answering from the instances that did respond would report "not requested" for titles the silent one still holds active requests for, and for a deletion rule that is the difference between keeping and deleting. Rule previews show the same warning rather than a normal empty result.

### Seerr Requester Watch State

Two fields answer two different questions, and most rules want the first:

| Field | `is true` means |
| --- | --- |
| `Seerr requester has watched` | A requester watched it. The request date is not consulted. |
| `Seerr requester watched after requesting` | The same, and every qualifying play came after that requester's **earliest** request for the season. |

What "watched it" means depends on the rule target:

| Scope         | Watched means                                            |
| ------------- | -------------------------------------------------------- |
| Movie version | A requester watched the movie                            |
| Episode       | A requester watched that episode                         |
| Season        | One requester watched every local episode in that season |
| Series        | One requester watched every regular local episode        |

Season 0 specials are excluded from series completion. Progress from different requesters is never combined to complete a season or series.

The two fields used to be one, and the fused version caused false negatives: a requester who finished a season could still read as `false` if anything created a newer request row for it. Seerr writes a separate request for a 4K copy and for every re-request of an airing season, so the bar could move past plays that had already happened. The bar is now that requester's earliest request, and rules saved before the split were migrated to `Seerr requester watched after requesting` so none of them changed what they match.

`Seerr requester watched after requesting` also needs a request record for the season it is judging; a season the requester never asked for cannot satisfy it. `Seerr requester has watched` has no such requirement.

That comparison is only as good as the request dates behind it. A Seerr that was rebuilt, migrated between instances, or simply re-requested writes rows dated after the plays they describe, and the field then reads `false` for an entire library no matter how the identity join resolves. **Settings -> User Signals -> Ignore Seerr Request Dates** turns the date comparison off, leaving `Seerr requester watched after requesting` to check completion alone -- the same question `Seerr requester has watched` answers. It is off by default, applies to every rule at once, and relaxes only that one half: an item nobody finished stays `false`, and an unreadable media server stays unknown. The **Why?** dialog says when the switch is on, and still lists which plays predate the request so the raw comparison remains visible.

Leave the switch off if your request dates are trustworthy and you want date-free matching in only some rules -- use `Seerr requester has watched` in those rules instead.

Both fields count the episodes you actually have, unlike `Season fully watched` below, which counts Sonarr's full known inventory. The difference is intentional: a requester can only watch what exists, so an unaired or missing episode must not make a season they did finish read as unwatched. A rule using both conditions therefore applies the stricter Sonarr-inventory test through `Season fully watched`.

Reclaimerr matches Seerr users automatically using the usernames, display names, email addresses, and linked Plex or Jellyfin account names from Seerr's user directory. Each playback provider also reports every name it knows an account by -- a Plex account id, username, title, friendly name, and email, or the equivalent from Jellyfin, Emby, Tautulli, and Tracearr -- so matching any one of those names reaches the rest.

When several providers describe the same media server -- a Plex server, the Tautulli watching it, and a Plex-bound Tracearr -- their directories are merged into one account per person, so a Plex title and the Plex username underneath it need no manual mapping. Two rules keep that safe: a name that a single provider gives to two of its own users bridges nothing, and provider ids never bridge across providers, because Plex numbering its owner `1` and Tautulli numbering its first user `1` are unrelated facts.

Explicit requester watch-user mappings under **Settings -> User Signals -> Seerr Requester to Watch User Mapping** remain available for identities that share no name at all. That screen lists every Seerr user, shows how many are covered automatically, and offers a picker with one entry per playback account -- ids, emails, and a Tracearr identity are shown as that account's other names rather than as entries of their own, and all of them are searchable. Do not confuse it with **Settings -> Users -> Sign-In Identity Links**, which links media-server logins to local Reclaimerr accounts and has no effect on rules. Comparisons are case-insensitive, and aliases only bridge accounts on the same media server. Tautulli and Plex-bound Tracearr identities are treated as Plex identities when applying provider-scoped mappings.

Each mapping also names which Seerr its requester belongs to. Picking a user from the lookup settles that for you. Leaving it on **Any instance** matches the _named_ user on every Seerr, which is what someone holding an account on both under one name wants -- so an unscoped mapping matches on the username, not on a user ID, since a user ID names a different person on each instance. Choosing **Any instance** clears the selected user for the same reason.

Requester watch state combines completed per-user playback snapshots from Plex, Jellyfin, and Emby with Tautulli or Tracearr events whose provider-native watched status is complete. Each provider's configured watched threshold remains the source of truth. Jellyfin and Emby Playback Reporting events describe activity but do not expose a reliable completion signal, so they do not independently satisfy this field. They remain available to the general `playback.*` fields. When the same completed play is available from multiple sources, Reclaimerr keeps the latest qualifying timestamp.

Previews refresh the watch snapshot when it is more than 15 minutes old and cleanup scans require a current one, so a manual `Sync Media` is no longer needed after changing identity mappings. Plex can provide current completed-watch state directly; durable completed Plex history requires Tautulli or a Plex-bound Tracearr server.

When a media server holding an item cannot report completion -- its watch snapshot has never synced, or its last attempt failed, and no retained durable history covers it -- these fields are **unknown** for that item rather than false. Unknown matches neither `is true` nor `is false`, so a broken media server can no longer make a cleanup rule delete media that was watched. The preview reports how many items were affected.

Use the **Why?** control on a preview row to see exactly how either field resolved: who requested the item and when, every name tried on their behalf, every completed play found with its timestamp, and -- when the answer is false -- which episodes were never watched versus which were watched before the request.

`Playback users` is a different question again. It lists who has played the item at all, with no completion requirement and no connection to who requested it, so it is not a substitute for either requester-watch field.

### Sonarr Rule Data

`Season fully watched` and `Season watched (%)` use Sonarr's complete known episode inventory as their denominator. Episodes Sonarr knows about still count when they are unaired or do not have files, so six watched episodes out of seven known episodes is 85.71%, not complete. Run `Sync Media` after upgrading or after Sonarr discovers new episodes.

Sonarr series are matched by TMDB id, falling back to TVDB id when Sonarr reports no TMDB id for a show. If a season has no successfully synchronized Sonarr episode inventory, its watch completion is unknown. Boolean and numeric completion conditions do not match that season; Reclaimerr does not fall back to treating the currently downloaded episodes as the complete season. Preview warnings count only seasons where missing inventory affects the current rule after its other conditions are applied, and show up to five example titles to aid troubleshooting.

`Sonarr series status` exposes Sonarr's canonical series status independently from the TMDB-backed `Series status` field. It is available to series, season, and episode rules with the values `continuing`, `ended`, `upcoming`, and `deleted`.

Status is loaded from Sonarr's bulk series response and does not require episode requests. When a series maps to multiple Sonarr instances, every mapped and reachable instance must report the same status. Missing or conflicting values are unknown and fail closed.

The latest-season fields are available only to whole-series rules:

| Field | Meaning |
| --- | --- |
| Latest season has unaired episodes | The latest regular Sonarr season has an episode airing later |
| Latest season has finale | The latest regular season has a `season` or `series` finale tag |

Reclaimerr ignores season 0 and checks only the highest-numbered regular season. This keeps scans efficient while covering upcoming seasons and split cours. Episode monitoring status is not considered.

Sonarr's series statistics may provide a future `nextAiring` value. Reclaimerr uses that value to prove that an unaired episode exists without requesting the season's episodes. A missing `nextAiring` value cannot prove that no future episode exists, so Reclaimerr requests only the latest season's episodes when the rule result still depends on Sonarr data.

Episode-state data is loaded only when an enabled rule uses one of these fields. Requests are cached for the current preview or cleanup scan and are limited to eight concurrent episode requests per Sonarr instance.

Unavailable, empty, or malformed Sonarr data is treated as unknown. Unknown values match neither `is true`, `is false`, `exists`, nor `does not exist`. They cannot create a cleanup candidate. Existing automated protections are preserved for the affected rule and series until Sonarr can be evaluated again.

When a series is mapped to multiple Sonarr instances, `true` wins if any instance proves it. `false` is returned only when every mapped instance successfully reports false. Otherwise the value remains unknown.

Typical protection rules are:

```text
Latest season has unaired episodes is true
```

```text
Latest season has finale is false
```

The finale field depends on Sonarr's metadata and may remain false while a season is incomplete or its finale metadata has not been updated. Combine it with status, age, watch-history, or library conditions and inspect the preview before enabling the rule.

### Playback Activity and History

Playback fields are available to movie-version, series, season, and episode rules. Reclaimerr stores current completed watch state directly from Jellyfin and Emby, then supplements it with compact events imported from the Jellyfin/Emby Playback Reporting plugin and Tautulli:

| Field | Meaning |
| --- | --- |
| Playback activity | Native completed watch state or a qualifying event |
| Playback plays | Highest native or imported play count |
| Playback duration | Total qualifying imported playback minutes |
| Longest playback | Longest qualifying imported playback in minutes |
| Playback user count | Current native watched users, or imported users when no native state exists |
| Playback users | Current native watched users, or imported users when no native state exists |
| Last playback activity | Most recent native or imported timestamp |
| Days since playback activity | Whole days since the most recent timestamp |

Movie events shorter than 15 seconds and episode events shorter than 7 seconds are ignored by default. These thresholds prevent brief scrubs from counting as activity, and can be changed in **Settings → General → Minimum Playback Duration**.

The thresholds are applied while history is imported, so a change only affects events imported after it. History already imported keeps the events it was imported with: raising the minimum does not retire short events that are already stored, and lowering it does not bring back events that were skipped.

#### Per-user playback conditions

The fields above are aggregated across every user who played the media, so `Longest playback > 30` matches if _anyone_ watched 30+ minutes, even if the specific person you care about only watched a few seconds. Two additional fields scope playback to one or more chosen users instead:

| Field | Meaning | Available scopes |
| --- | --- | --- |
| Playback duration by user (minutes) | Selected user's total watched minutes for the target | Movie version, series, season, episode |
| Playback watched by user (%) | Selected user's total watched time as a percent of runtime | Movie version, episode only |

Both fields require picking at least one user from a user-picker in the rule editor (there is no "any user" option - use the aggregate `Playback users` / `Longest playback` fields above for that). When more than one user is selected, each one is compared on their own and the condition matches as soon as any of them satisfies it, so `under 5 minutes by alice or bob` is true when either of them is under 5 minutes. A selected user with no recorded activity counts as 0, not unknown, on any target imported history covers. Both fields read the same imported events the aggregate duration fields do, so a target with no Playback Reporting or Tautulli history behind it is unknown rather than watched by nobody, and the condition does not match it.

`Playback watched by user (%)` is only available for movie-version and episode rules, since percent-watched needs a known runtime and only movie files and episodes have one on record (series and season rules can still use the minutes field). Episode runtimes are recorded during a media-server sync, so on an existing install episode percent rules stay unknown until the next sync has run.

Both fields sum a user's playback across all of their sessions for the target, so resuming a paused movie across several sittings still counts toward the total. That also means percent is time watched rather than furthest position reached: re-watching pushes a user past 100%, and re-watching the same twenty minutes repeatedly accrues percent without the rest of the film ever being seen. Percent is measured against the runtime of the movie file the rule is evaluating, so on a movie kept in both a theatrical and an extended version the same watch time reads as a higher percent on the shorter file.

Events are retained locally until their source service configuration is deleted. They are mapped by exact media-server IDs and stable TMDB, season-number, and episode-number identities, so imported history can survive provider retention cleanup and media deletion/re-addition. Title-only matching is never used.

The existing `watch.*` fields continue to describe the current library copy. Native Jellyfin/Emby playback is current completed state and is refreshed by the **Refresh Playback Data** task every 15 minutes by default as well as by Sync Media. Imported plugin/Tautulli events are durable history and may include activity from before the current copy was added. For Jellyfin and Emby, native state is authoritative for **Playback users** and **Playback user count**: old Playback Reporting events do not keep a user matched after the media server no longer marks that user as watched. For a movie version or episode this is the exact item; for a season or series it means the user has completed at least one available local episode. This is not a per-user full-season/series completion check. When the same media is linked across servers, Reclaimerr combines each server's authoritative users; Jellyfin or Emby native state does not replace Plex users imported from Tautulli.

`Playback activity` is either true or false when an applicable native snapshot or imported-history provider can observe the media target. Targets outside that coverage are unknown and match neither value. Marking an item watched without playing it is captured by Jellyfin/Emby's native current state after the next playback data refresh, but it does not create an imported playback event.

Playback data is loaded only when an enabled rule uses a `playback.*` field. Native snapshots are read from the database. A preview refreshes them when they are more than 15 minutes old, while cleanup scans require a current snapshot. Tautulli history is fetched in one ungrouped paginated pass. Playback Reporting and Tautulli imports use an overlap window and event keys to avoid duplicate events during incremental refreshes. Tracearr uses its cursor-paginated public API v2 history and user endpoints, retaining unfinished play chains until their completed rows arrive.

For each media server, Reclaimerr uses exactly one durable history provider for playback totals. A Tracearr binding replaces Tautulli or Playback Reporting for that server; their retained rows are excluded from aggregates while the binding is active, because counting the same play from two providers would inflate every total. A Tracearr failure is reported as unavailable and does not fall back to the other provider. Native Jellyfin/Emby snapshots continue to supplement durable history.

Completion evidence is the exception. `Seerr requester has watched` asks whether a play finished, not how many plays there were, so a completed play retained from a superseded provider still counts for that field. Binding Tracearr to a Plex server therefore keeps the older Tautulli history that predates Tracearr instead of quietly reporting that media as never watched. Rows from Tracearr servers that are no longer bound are still ignored.

Playback-user rules match usernames case-insensitively and can require any, none, or all selected users. Jellyfin and Emby resolve names from their native user APIs; Tautulli resolves only the Plex history it supplies. If a native Jellyfin/Emby snapshot is unavailable, user conditions are unknown rather than falling back to stale imported events. If any user on an item cannot be resolved, username conditions remain unavailable for that item while the other playback metrics remain usable.

If a native watched item has no usable last-played timestamp, activity and count remain available while the last-activity fields stay unknown.

Plex playback fields, including playback-user rules, require Tautulli or a Plex-bound Tracearr server. Jellyfin/Emby activity, count, users, and latest-watch fields work without the Playback Reporting plugin after a playback data refresh; duration fields still require imported plugin history. Reclaimerr does not currently import playback events directly from Plex.

If no applicable source is configured, a native snapshot refresh fails, an import request fails, or an item cannot be observed through an available source, playback values are unknown. Unknown values cannot create cleanup candidates. Existing automated protections are preserved until the affected playback rule can be evaluated again.

Immediately before automatic deletion, eligible candidates whose matched rules use playback fields are checked again against freshly refreshed data. An item that was watched after becoming a candidate, or whose playback state cannot be observed safely, is not authorized for deletion by that playback rule.

## Validation and Editing

- Operator choices are limited to operators supported by the selected field.
- Field choices are limited to the selected target scope.
- Changing a field resets an incompatible operator to the field's default.
- Existing saved rules are not rewritten until they are edited and saved.
- Backend validation rejects unsupported field, operator, or scope combinations.
- Rule preview uses the same evaluation logic as cleanup candidate scans.

## Recommended Workflow

1. Choose whether the rule creates cleanup candidates or automated protections.
2. Select the narrowest target scope that represents the intended item.
3. Add positive conditions that identify the media.
4. Add quality, age, watch-history, or metadata conditions to reduce broad matches.
5. Preview the rule and inspect the displayed actual values.
6. Save the rule only after the preview contains the intended files.
7. Run a cleanup scan to reconcile candidates and automated protections.

## Related Pages

- [How It Works](how-it-works.md)
- [Tasks](tasks.md)
- [External API](../reference/api.md)
- [Internal UI API](../reference/internal-api.md)
