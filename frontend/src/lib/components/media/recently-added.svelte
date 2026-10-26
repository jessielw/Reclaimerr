<script lang="ts">
  import { get_api } from "$lib/api";
  import { auth } from "$lib/stores/auth";
  import { hasPageAccess } from "$lib/page-access";
  import PosterRow, {
    type PosterRowEntry,
  } from "$lib/components/media/poster-row.svelte";
  import MediaDetailDialog from "$lib/components/media/media-detail-dialog.svelte";
  import DeleteRequestDialog from "$lib/components/media/delete-request-dialog.svelte";
  import ProtectionRequestDialog from "$lib/components/media/protection-request-dialog.svelte";
  import { formatDate, formatDistanceToNow } from "$lib/utils/date";
  import {
    MediaType,
    PageAccess,
    type MediaItem,
    type MovieWithStatus,
    type PaginatedResponse,
    type SeriesWithStatus,
  } from "$lib/types/shared";

  type RecentEntry = PosterRowEntry<MediaItem>;

  let row = $state<PosterRow<MediaItem>>();

  // dialogs
  let showDetailDialog = $state(false);
  let showExceptionDialog = $state(false);
  let showDeleteDialog = $state(false);
  let selected = $state<RecentEntry | null>(null);

  const canViewMovies = $derived(hasPageAccess($auth.user, PageAccess.Movies));
  const canViewSeries = $derived(hasPageAccess($auth.user, PageAccess.Series));

  const toEntry = (media: MediaItem, mediaType: MediaType): RecentEntry => {
    const name = media.year ? `${media.title} (${media.year})` : media.title;
    const kind = mediaType === MediaType.Movie ? "Movie" : "Series";
    const tooltip = [name, `${kind} • Added ${formatDate(media.added_at!)}`];
    if (media.seerr_requesters.length > 0) {
      const names = media.seerr_requesters.map((r) => r.display_name);
      tooltip.push(`Requested by ${names.join(", ")}`);
    }
    return {
      key: `${mediaType}:${media.id}`,
      mediaType,
      title: media.title,
      subtitle: formatDistanceToNow(media.added_at!),
      posterUrl: media.poster_url,
      tooltip,
      data: media,
    };
  };

  const fetchNewest = async (
    mediaType: MediaType,
    count: number,
  ): Promise<RecentEntry[]> => {
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
    return data.items
      .filter((media) => media.added_at)
      .map((media) => toEntry(media, mediaType));
  };

  // pull the newest of each type, then merge so the row is one timeline
  const fetchEntries = async (count: number): Promise<RecentEntry[]> => {
    const results = await Promise.all([
      canViewMovies ? fetchNewest(MediaType.Movie, count) : [],
      canViewSeries ? fetchNewest(MediaType.Series, count) : [],
    ]);
    return results
      .flat()
      .sort((a, b) => b.data.added_at!.localeCompare(a.data.added_at!))
      .slice(0, count);
  };

  // request dialogs fire onSuccess once per created request, all after they
  // settle, so a single reload already reflects every one of them
  let refreshing = false;
  const refreshAfterRequest = async () => {
    if (refreshing || !row) return;
    refreshing = true;
    try {
      const entries = await row.reload();
      // keep the open detail dialog in sync with the reloaded status
      if (selected) {
        const { key } = selected;
        selected = entries.find((e) => e.key === key) ?? selected;
      }
    } finally {
      refreshing = false;
    }
  };

  const openDetails = (entry: RecentEntry) => {
    selected = entry;
    showDetailDialog = true;
  };
</script>

{#if canViewMovies || canViewSeries}
  <PosterRow
    bind:this={row}
    title="Recently Added"
    storageKey="dashboard_recently_added"
    emptyText="Nothing added recently"
    {fetchEntries}
    onSelect={openDetails}
  />

  {#if selected}
    <MediaDetailDialog
      bind:open={showDetailDialog}
      media={selected.data}
      mediaType={selected.mediaType}
      onRequestException={() => (showExceptionDialog = true)}
      onRequestDelete={() => (showDeleteDialog = true)}
    />

    <ProtectionRequestDialog
      bind:open={showExceptionDialog}
      media={selected.data}
      mediaType={selected.mediaType}
      onSuccess={refreshAfterRequest}
    />

    <DeleteRequestDialog
      bind:open={showDeleteDialog}
      media={selected.data}
      mediaType={selected.mediaType}
      onSuccess={refreshAfterRequest}
    />
  {/if}
{/if}
