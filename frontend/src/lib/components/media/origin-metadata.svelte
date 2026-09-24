<script lang="ts">
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import type {
    ArrRef,
    CandidatePlaybackWatcher,
    CandidatePlaybackWatchers,
    SeerrLink,
    SeerrRequester,
  } from "$lib/types/shared";

  interface Props {
    arrRefs?: ArrRef[];
    arrTags?: string[];
    seerrLinks?: SeerrLink[];
    seerrRequesters?: SeerrRequester[];
    playbackWatchers?: CandidatePlaybackWatchers | null;
    compact?: boolean;
    class?: string;
  }

  let {
    arrRefs = [],
    arrTags = [],
    seerrLinks = [],
    seerrRequesters = [],
    playbackWatchers = null,
    compact = false,
    class: className = "",
  }: Props = $props();

  const hasContent = $derived(
    arrRefs.length > 0 ||
      arrTags.length > 0 ||
      seerrLinks.length > 0 ||
      seerrRequesters.length > 0 ||
      playbackWatchers !== null,
  );

  // Past this many names the row wraps into a wall; the rest go in a tooltip.
  const WATCHER_NAME_LIMIT = 6;

  const shownWatchers = $derived(
    playbackWatchers?.users.slice(0, WATCHER_NAME_LIMIT) ?? [],
  );
  const hiddenWatchers = $derived(
    playbackWatchers?.users.slice(WATCHER_NAME_LIMIT) ?? [],
  );
  // Users the provider counted but could not name.
  const unnamedWatcherCount = $derived(
    playbackWatchers
      ? Math.max(0, playbackWatchers.user_count - playbackWatchers.users.length)
      : 0,
  );

  const shortDate = (value: string | null): string | null => {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date.toLocaleDateString();
  };

  // A null count means only the media server's own watched state names this
  // user: they watched it, but how often is unknown.
  const watcherLabel = (user: CandidatePlaybackWatcher): string =>
    user.play_count == null
      ? `${user.name} ✓`
      : `${user.name} (${user.play_count})`;

  const watcherTitle = (user: CandidatePlaybackWatcher): string | undefined => {
    if (user.play_count == null) {
      return "Marked watched on the media server; play count unknown";
    }
    const last = shortDate(user.last_activity_at);
    return last ? `Last watched ${last}` : undefined;
  };

  const watcherSummary = $derived.by(() => {
    if (!playbackWatchers) return "";
    const plays = playbackWatchers.play_count;
    const parts = [`${plays} play${plays === 1 ? "" : "s"}`];
    const last = shortDate(playbackWatchers.last_activity_at);
    if (last) parts.push(`last ${last}`);
    return parts.join(", ");
  });

  // One Seerr needs no disambiguation; several do.
  const seerrLabel = (link: SeerrLink) =>
    seerrLinks.length > 1
      ? (link.service_name ?? `Seerr #${link.service_config_id}`)
      : "Seerr";

  const requesterInstances = $derived(
    new Set(seerrRequesters.map((requester) => requester.service_config_id))
      .size,
  );
</script>

{#if hasContent}
  <section
    class={`${
      compact
        ? "space-y-1.5 text-xs"
        : "rounded border border-border/70 bg-muted/20 p-3 text-sm"
    } ${className}`}
  >
    {#if !compact}
      <h4 class="mb-1.5 text-xs uppercase tracking-wide text-muted-foreground">
        Source Information
      </h4>
    {/if}

    <div class="space-y-1.5">
      {#if arrRefs.length > 0 || seerrLinks.length > 0}
        <div class="flex flex-wrap items-center gap-1.5">
          <span class="text-muted-foreground">Open in:</span>
          {#each arrRefs as ref (`${ref.service_config_id}-${ref.arr_id}`)}
            {@const label = ref.service_name ?? ref.service_type}
            {#if ref.item_url}
              <a
                href={ref.item_url}
                target="_blank"
                rel="noopener noreferrer"
                class="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-0.5 font-medium text-foreground hover:border-primary hover:text-primary"
              >
                {label}
                <ExternalLink class="size-3" />
              </a>
            {:else}
              <span
                class="inline-flex items-center rounded-md border border-border bg-card px-2 py-0.5 font-medium text-muted-foreground"
                title="Run Sync Media to populate this deep link"
              >
                {label}
              </span>
            {/if}
          {/each}
          {#each seerrLinks as link (link.service_config_id)}
            {#if link.item_url}
              <a
                href={link.item_url}
                target="_blank"
                rel="noopener noreferrer"
                class="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-0.5 font-medium text-foreground hover:border-primary hover:text-primary"
              >
                {seerrLabel(link)}
                <ExternalLink class="size-3" />
              </a>
            {/if}
          {/each}
        </div>
      {/if}

      {#if seerrRequesters.length > 0}
        <div class="flex flex-wrap items-center gap-1.5">
          <span class="text-muted-foreground">Requested by:</span>
          {#each seerrRequesters as requester (requester.key)}
            {@const handle =
              requester.username &&
              requester.username !== requester.display_name
                ? `@${requester.username}`
                : null}
            {@const instance =
              requesterInstances > 1
                ? (requester.service_name ??
                  `Seerr #${requester.service_config_id}`)
                : null}
            {@const requested = shortDate(requester.requested_at)}
            <Badge
              variant="secondary"
              title={[handle, instance].filter(Boolean).join(" | ") ||
                undefined}
            >
              {requester.display_name}
              {#if requested}
                <span class="font-normal text-muted-foreground">
                  · {requested}
                </span>
              {/if}
            </Badge>
          {/each}
        </div>
      {/if}

      {#if playbackWatchers}
        <div class="flex flex-wrap items-center gap-1.5">
          <span class="text-muted-foreground">Watched by:</span>
          {#each shownWatchers as user (user.name)}
            <Badge
              variant="secondary"
              class={user.play_count == null ? "text-muted-foreground" : ""}
              title={watcherTitle(user)}
            >
              {watcherLabel(user)}
            </Badge>
          {/each}
          {#if hiddenWatchers.length > 0}
            <Badge
              variant="outline"
              title={hiddenWatchers.map(watcherLabel).join(", ")}
            >
              +{hiddenWatchers.length} more
            </Badge>
          {/if}
          {#if unnamedWatcherCount > 0}
            <Badge
              variant="outline"
              title="Plays the playback provider recorded without a username"
            >
              {shownWatchers.length > 0 ? "+" : ""}{unnamedWatcherCount} unnamed
            </Badge>
          {/if}
          <span class="text-muted-foreground">({watcherSummary})</span>
        </div>
      {/if}

      {#if arrTags.length > 0}
        <div class="flex flex-wrap items-center gap-1.5">
          <span class="text-muted-foreground">Arr tags:</span>
          {#each arrTags as tag (tag)}
            <Badge variant="outline">{tag}</Badge>
          {/each}
        </div>
      {/if}
    </div>
  </section>
{/if}
