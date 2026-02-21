<script lang="ts">
  import MediaCard from "./MediaCard.svelte";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import ChevronLeft from "@lucide/svelte/icons/chevron-left";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
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
    onRequestException?: (media: MediaItem) => void;
    onViewDetails?: (media: MediaItem) => void;
    onPageChange?: (page: number) => void;
  }

  let {
    data,
    mediaType,
    loading = false,
    error = "",
    onRequestException,
    onViewDetails,
    onPageChange,
  }: Props = $props();

  // pagination handlers
  const handlePreviousPage = () => {
    if (data && data.page > 1 && onPageChange) {
      onPageChange(data.page - 1);
    }
  };

  const handleNextPage = () => {
    if (data && data.page < data.total_pages && onPageChange) {
      onPageChange(data.page + 1);
    }
  };
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
      <p class="text-muted-foreground text-lg">No {mediaType}s found</p>
    </div>
  {:else}
    <!-- grid -->
    <div
      class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4 mb-8"
    >
      {#each data.items as media (media.id)}
        <MediaCard {media} {mediaType} {onRequestException} {onViewDetails} />
      {/each}
    </div>

    <!-- pagination -->
    {#if data.total_pages > 1}
      <div class="flex justify-between items-center">
        <p class="text-sm text-muted-foreground">
          Showing {(data.page - 1) * data.per_page + 1} to {Math.min(
            data.page * data.per_page,
            data.total,
          )} of {data.total} items
        </p>

        <div class="flex gap-2 items-center">
          <Button
            variant="outline"
            size="sm"
            onclick={handlePreviousPage}
            disabled={data.page === 1}
          >
            <ChevronLeft class="w-4 h-4 mr-1" />
            Previous
          </Button>

          <span class="text-sm text-muted-foreground">
            Page {data.page} of {data.total_pages}
          </span>

          <Button
            variant="outline"
            size="sm"
            onclick={handleNextPage}
            disabled={data.page === data.total_pages}
          >
            Next
            <ChevronRight class="w-4 h-4 ml-1" />
          </Button>
        </div>
      </div>
    {/if}
  {/if}
</div>
