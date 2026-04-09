[x] - Settings screen is a little confusing - not immediately obvious that the Jellyfin page you land on is part of the 'Services' tab above. Maybe a tab bar on the left instead with highlighted panels to show where you are?

[x] - Plex API key isn't called API key

[x] - Plex library sync seemed a bit flaky - clicking sync libraries didn't load any, then clicking save disabled the server, and then another save made the libraries show up

[x] - (for media servers) - Saving settings reloads the whole panel, would be good if the panel didn't need to reload

[x] - If you add client credentials and hit save without enabling, the popup says '...client disabled', which sounds like it has been disabled. Maybe reword the popups for both enabled and disabled states, or just remove the 'enabled' messaging from the popups entirely

[x] - I'd rename Aarr Tagging to 'Radarr and Sonarr tagging'

[x] -- Tasks show that tasks will run on a schedule when the service hasn't been set up - eg Jellyfin when no Jellyfin server is configured.

[x] - Relevant tasks don't seem to auto-run when a new service is added - eg, you'd expect a 'Sync Plex Media' task to run when a Plex server is added

[x] - The percentages on dashboard are a bit misleading, as they're just on numbers of seasons vs number of movies - using data % might be a fairer reflection?

[x] - Dashboard - Plex shows last sync: never even when manually synced via task run

[x] - Add a filter on movies/tv screens to only show reclaim candidates

[x] - Add a slider to adjust the scale of the movies/tv posters so you can fit more rows/columns per user preference

[x] - Amended or removing a rule then re-running the scan tasks doesn't change which items have been marked for reclaiming

[x] - "Unwatched" in rules isn't clear on who that's unwatched by - the server owner, home users, all users, etc

[x] - Allow dashboard to switch data display to MB/GB/TB, eg 1.98 TB instead of 1981.64 GB

[x] - Adjusting a task schedule to a custom one, then using reset default, results in it being in a bugged state and requires reset being pressed twice

[ ] - Ensure house keeping tasks are keeping things nice and clean

[x] - TODO (next): Per-season series deletion - currently candidates.svelte deletes entire series. Should support selecting individual seasons for deletion.
