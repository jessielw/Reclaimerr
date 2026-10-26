<script lang="ts">
  import { onMount } from "svelte";
  import { get_api } from "$lib/api";
  import { auth } from "$lib/stores/auth";
  import { hasPageAccess } from "$lib/page-access";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import MediaDetailDialog from "$lib/components/media/media-detail-dialog.svelte";
  import DeleteRequestDialog from "$lib/components/media/delete-request-dialog.svelte";
  import ProtectionRequestDialog from "$lib/components/media/protection-request-dialog.svelte";
  import * as Select from "$lib/components/ui/select/index.js";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import ChevronDown from "@lucide/svelte/icons/chevron-down";
  import { formatDate, formatDistanceToNow } from "$lib/utils/date";
  import { createFilterState } from "$lib/utils/pagination";
  import {
    MediaType,
    PageAccess,
    type MediaItem,
    type MovieWithStatus,
    type PaginatedResponse,
    type SeriesWithStatus,
  } from "$lib/types/shared";

  const COUNT_OPTIONS = [6, 12, 24];

  const _countStore = createFilterState(
    "dashboard_recently_added_count",
    12,
    (v): v is number => COUNT_OPTIONS.includes(v as number),
  );
  const _collapsedStore = createFilterState(
    "dashboard_recently_added_collapsed",
    false,
  );
  let count = $state(_countStore.getInitial());
  let collapsed = $state(_collapsedStore.getInitial());
  $effect(() => _countStore.save(count));
  $effect(() => _collapsedStore.save(collapsed));

  type RecentEntry = { media: MediaItem; mediaType: MediaType };

  let entries = $state<RecentEntry[]>([]);
  let loading = $state(true);
  let error = $state("");

  // dialogs
  let showDetailDialog = $state(false);
  let showExceptionDialog = $state(false);
  let showDeleteDialog = $state(false);
  let selected = $state<RecentEntry | null>(null);

  const canViewMovies = $derived(hasPageAccess($auth.user, PageAccess.Movies));
  const canViewSeries = $derived(hasPageAccess($auth.user, PageAccess.Series));

  const fetchNewest = async (mediaType: MediaType): Promise<RecentEntry[]> => {
    const endpoint =
      mediaType === MediaType.Movie ? "/api/media/movies" : "/api/media/series";
    const params = new URLSearchParams({
      page: "1",
      per_page: count.toString(),
      sort_by: "added_at",
      sort_order: "desc",
    });
    const data = await get_api<
      PaginatedResponse<MovieWithStatus | SeriesWithStatus>
    >(`${endpoint}?${params.toString()}`);
    return data.items.map((media) => ({ media, mediaType }));
  };

  // pull the newest of each type, then merge so the row is one timeline
  let loadSeq = 0;
  const load = async () => {
    const seq = ++loadSeq;
    error = "";
    try {
      const results = await Promise.all([
        canViewMovies ? fetchNewest(MediaType.Movie) : [],
        canViewSeries ? fetchNewest(MediaType.Series) : [],
      ]);
      // a newer load (count changed mid-flight) owns the row now
      if (seq !== loadSeq) return;
      entries = results
        .flat()
        .filter((entry) => entry.media.added_at)
        .sort((a, b) => b.media.added_at!.localeCompare(a.media.added_at!))
        .slice(0, count);
    } catch (err: any) {
      console.error("Error loading recently added media:", err);
      error = err.message;
    } finally {
      loading = false;
    }
  };

  // request dialogs fire onSuccess once per created request, all after they
  // settle, so a single reload already reflects every one of them
  let refreshing = false;
  const refreshAfterRequest = async () => {
    if (refreshing) return;
    refreshing = true;
    try {
      await load();
      // keep the open detail dialog in sync with the reloaded status
      if (selected) {
        const { media, mediaType } = selected;
        selected =
          entries.find(
            (e) => e.mediaType === mediaType && e.media.id === media.id,
          ) ?? selected;
      }
    } finally {
      refreshing = false;
    }
  };

  const openDetails = (entry: RecentEntry) => {
    selected = entry;
    showDetailDialog = true;
  };

  const handleRequestException = () => {
    showExceptionDialog = true;
  };

  const handleRequestDelete = () => {
    showDeleteDialog = true;
  };

  const tooltipLines = ({ media, mediaType }: RecentEntry): string[] => {
    const name = media.year ? `${media.title} (${media.year})` : media.title;
    const kind = mediaType === MediaType.Movie ? "Movie" : "Series";
    const lines = [name, `${kind} • Added ${formatDate(media.added_at!)}`];
    if (media.seerr_requesters.length > 0) {
      const names = media.seerr_requesters.map((r) => r.display_name);
      lines.push(`Requested by ${names.join(", ")}`);
    }
    return lines;
  };

  // load on mount, then again whenever the row is expanded or resized; the
  // previous entries stay on screen while the new ones come in
  let mounted = $state(false);
  $effect(() => {
    count;
    if (mounted && !collapsed && (canViewMovies || canViewSeries)) load();
  });

  onMount(() => {
    mounted = true;
  });
</script>

{#if canViewMovies || canViewSeries}
  <section class="bg-card rounded-lg border border-border p-5">
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-xl font-semibold text-foreground">Recently Added</h2>
      <div class="flex items-center gap-2">
        {#if !collapsed}
          <Select.Root
            type="single"
            value={count.toString()}
            onValueChange={(v) => (count = parseInt(v))}
          >
            <Select.Trigger
              size="sm"
              class="w-20 bg-card text-card-foreground"
              aria-label="Titles to show"
            >
              {count}
            </Select.Trigger>
            <Select.Content class="bg-card">
              {#each COUNT_OPTIONS as option}
                <Select.Item
                  value={option.toString()}
                  label={option.toString()}
                  class="text-card-foreground"
                >
                  {option}
                </Select.Item>
              {/each}
            </Select.Content>
          </Select.Root>
        {/if}
        <button
          type="button"
          class="rounded-md p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground
            transition-colors cursor-pointer"
          aria-label={collapsed
            ? "Expand recently added"
            : "Collapse recently added"}
          aria-expanded={!collapsed}
          onclick={() => (collapsed = !collapsed)}
        >
          <ChevronDown
            class={`size-5 transition-transform ${collapsed ? "-rotate-90" : ""}`}
          />
        </button>
      </div>
    </div>

    {#if collapsed}
      <!-- collapsed: header only, nothing fetched -->
    {:else if loading}
      <p class="mt-4 text-muted-foreground text-center py-8">Loading...</p>
    {:else if error}
      <p class="mt-4 text-destructive text-center py-8">{error}</p>
    {:else if entries.length === 0}
      <p class="mt-4 text-muted-foreground text-center py-8">
        Nothing added recently
      </p>
    {:else}
      <div class="mt-4 flex gap-3 overflow-x-auto pb-2">
        {#each entries as entry (`${entry.mediaType}:${entry.media.id}`)}
          <Tooltip.Root>
            <Tooltip.Trigger
              class="w-28 shrink-0 text-left cursor-pointer [&_*]:cursor-pointer"
              onclick={() => openDetails(entry)}
            >
              <PosterThumb
                mediaType={entry.mediaType}
                posterUrl={entry.media.poster_url}
                posterSize="185"
                tailWindElSize="w-28"
                showMediaType
              />
              <p class="mt-1.5 text-sm font-medium text-foreground truncate">
                {entry.media.title}
              </p>
              <p class="text-xs text-muted-foreground truncate">
                {formatDistanceToNow(entry.media.added_at!)}
              </p>
            </Tooltip.Trigger>
            <Tooltip.Content>
              {#each tooltipLines(entry) as line}
                <p>{line}</p>
              {/each}
            </Tooltip.Content>
          </Tooltip.Root>
        {/each}
      </div>
    {/if}
  </section>

  {#if selected}
    <MediaDetailDialog
      bind:open={showDetailDialog}
      media={selected.media}
      mediaType={selected.mediaType}
      onRequestException={handleRequestException}
      onRequestDelete={handleRequestDelete}
    />

    <ProtectionRequestDialog
      bind:open={showExceptionDialog}
      media={selected.media}
      mediaType={selected.mediaType}
      onSuccess={refreshAfterRequest}
    />

    <DeleteRequestDialog
      bind:open={showDeleteDialog}
      media={selected.media}
      mediaType={selected.mediaType}
      onSuccess={refreshAfterRequest}
    />
  {/if}
{/if}
