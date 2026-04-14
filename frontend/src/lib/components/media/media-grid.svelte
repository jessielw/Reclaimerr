<script lang="ts">
  import MediaCard from "./media-card.svelte";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import CompactPagination from "$lib/components/compact-pagination.svelte";
  import type {
    MediaItem,
    MediaType,
    PaginatedResponse,
  } from "$lib/types/shared";

  interface Props {
    data: PaginatedResponse<MediaItem> | null;
    mediaType: MediaType;
    loading?: boolean;
    error?: string;
    posterSize?: number;
    onRequestException?: (media: MediaItem) => void;
    onViewDetails?: (media: MediaItem) => void;
    onPageChange?: (page: number) => void;
  }

  let {
    data,
    mediaType,
    loading = false,
    error = "",
    posterSize = 150,
    onRequestException,
    onViewDetails,
    onPageChange,
  }: Props = $props();
</script>

<div class="w-full">
  {#if loading}
    <div class="flex justify-center items-center py-20">
      <Spinner class="w-12 h-12" />
    </div>
  {:else if error}
    <div class="text-center py-20">
      <p class="text-red-500 text-lg">{error}</p>
    </div>
  {:else if !data || data.items.length === 0}
    <div class="text-center py-20">
      <p class="text-muted-foreground text-lg">
        No {mediaType === "movie" ? "movies" : "series"} found
      </p>
    </div>
  {:else}
    <!-- grid -->
    <div
      class="grid gap-4 mb-4"
      style="grid-template-columns: repeat(auto-fill, minmax({posterSize}px, 1fr))"
    >
      {#each data.items as media (media.id)}
        <MediaCard {media} {mediaType} {onRequestException} {onViewDetails} />
      {/each}
    </div>

    <!-- pagination -->
    {#if data.total_pages > 1}
      <div
        class="flex flex-wrap justify-center gap-2 md:flex-nowrap md:justify-between items-center"
      >
        <p class="text-sm text-muted-foreground">
          Showing {(data.page - 1) * data.per_page + 1} to {Math.min(
            data.page * data.per_page,
            data.total,
          )} of {data.total} items
        </p>

        <CompactPagination
          currentPage={data.page}
          totalPages={data.total_pages}
          maxVisiblePages={3}
          onPageChange={(page) => onPageChange?.(page)}
        />
      </div>
    {/if}
  {/if}
</div>
