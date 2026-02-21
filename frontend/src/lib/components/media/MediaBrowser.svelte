<script lang="ts">
  import { onMount } from "svelte";
  import { get_api } from "$lib/api";
  import { Input } from "$lib/components/ui/input/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import MediaGrid from "$lib/components/media/MediaGrid.svelte";
  import MediaDetailDialog from "$lib/components/media/MediaDetailDialog.svelte";
  import ExceptionRequestDialog from "$lib/components/media/ExceptionRequestDialog.svelte";
  import Search from "@lucide/svelte/icons/search";
  import {
    MediaType,
    type MovieWithStatus,
    type SeriesWithStatus,
    type MediaItem,
    type PaginatedResponse,
  } from "$lib/types/shared";
  import { toast } from "svelte-sonner";

  const sortByOptions = [
    { value: "title", label: "Title" },
    { value: "year", label: "Year" },
    { value: "added_at", label: "Date Added" },
    { value: "size", label: "Size" },
    { value: "vote_average", label: "Rating" },
  ];

  interface Props {
    mediaType: MediaType;
  }
  let { mediaType }: Props = $props();

  let loading = $state(false);
  let error = $state("");
  let mediaData = $state<PaginatedResponse<
    MovieWithStatus | SeriesWithStatus
  > | null>(null);

  // filters and search
  let searchQuery = $state("");
  let sortBy = $state("title");
  let sortOrder = $state("asc");
  let currentPage = $state(1);

  // dialogs
  let showDetailDialog = $state(false);
  let showExceptionDialog = $state(false);
  let selectedMedia = $state<MediaItem | null>(null);

  // debounce timer for search
  let searchTimer: ReturnType<typeof setTimeout> | null = null;

  // computed values based on mediaType
  const isMovie = $derived(mediaType === MediaType.Movie);
  const apiEndpoint = $derived(
    isMovie ? "/api/media/movies" : "/api/media/series",
  );
  const title = $derived(isMovie ? "Movies" : "Series");
  const description = $derived(
    isMovie
      ? "Browse and manage your movie library"
      : "Browse and manage your TV series library",
  );
  const searchPlaceholder = $derived(
    isMovie ? "Search movies..." : "Search series...",
  );

  // watch for changes in sortBy and sortOrder to reload
  $effect(() => {
    sortBy;
    sortOrder;
    loadMedia();
  });

  // load media from API with filters and pagination
  const loadMedia = async () => {
    try {
      loading = true;
      error = "";

      const params = new URLSearchParams({
        page: currentPage.toString(),
        per_page: "50",
        sort_by: sortBy,
        sort_order: sortOrder,
      });

      if (searchQuery.trim()) {
        params.append("search", searchQuery.trim());
      }

      const data = await get_api<
        PaginatedResponse<MovieWithStatus | SeriesWithStatus>
      >(`${apiEndpoint}?${params.toString()}`);

      mediaData = data;
      console.log("Loaded media data:", data);
    } catch (err: any) {
      console.error(`Error loading ${title.toLowerCase()}:`, err);
      error = err.message;
      toast.error(`Failed to load ${title.toLowerCase()}: ${err.message}`);
    } finally {
      loading = false;
    }
  };

  // handle search input with debounce
  const handleSearch = (event: Event) => {
    const target = event.target as HTMLInputElement;
    searchQuery = target.value;

    // debounce search
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      currentPage = 1;
      loadMedia();
    }, 500);
  };

  // when user changes page
  const handlePageChange = (page: number) => {
    currentPage = page;
    loadMedia();
  };

  // when user clicks on a media card to view details
  const handleViewDetails = (media: MediaItem) => {
    selectedMedia = media;
    showDetailDialog = true;
  };

  // when user clicks "Request Exception" from either media card or detail dialog
  const handleRequestException = (media: MediaItem) => {
    selectedMedia = media;
    showExceptionDialog = true;
  };

  // after successful exception request, reload media to get updated status
  const handleExceptionSuccess = () => {
    // reload media to get updated status
    loadMedia();
  };

  onMount(() => {
    loadMedia();
  });
</script>

<div class="p-8">
  <div class="max-w-450 mx-auto">
    <!-- header -->
    <div class="mb-8">
      <h1 class="text-3xl font-bold text-foreground mb-2">{title}</h1>
      <p class="text-muted-foreground">{description}</p>
    </div>

    <!-- filters and search -->
    <div class="mb-6 flex flex-col sm:flex-row gap-2">
      <!-- search -->
      <div class="relative flex-1">
        <Search
          class="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted-foreground"
        />
        <Input
          type="text"
          placeholder={searchPlaceholder}
          value={searchQuery}
          oninput={handleSearch}
          class="pl-10 bg-card text-card-foreground placeholder-text-muted-foreground"
        />
      </div>

      <!-- sort by -->
      <div class="flex flex-1 flex-row gap-4">
        <Select.Root type="single" bind:value={sortBy}>
          <Select.Trigger
            class="flex-10 bg-card border-ring text-card-foreground"
          >
            {sortByOptions.find((opt) => opt.value === sortBy)?.label}
          </Select.Trigger>
          <Select.Content class="bg-card border-ring">
            {#each sortByOptions as option}
              <Select.Item
                value={option.value}
                label={option.label}
                class="text-card-foreground"
              >
                {option.label}
              </Select.Item>
            {/each}
          </Select.Content>
        </Select.Root>

        <!-- sort order -->
        <Select.Root type="single" bind:value={sortOrder}>
          <Select.Trigger
            class="flex-10 bg-card border-ring text-card-foreground"
          >
            {sortOrder === "asc" ? "Ascending" : "Descending"}
          </Select.Trigger>
          <Select.Content class="bg-card border-ring">
            <Select.Item
              value="asc"
              label="Ascending"
              class="text-card-foreground">Ascending</Select.Item
            >
            <Select.Item
              value="desc"
              label="Descending"
              class="text-card-foreground">Descending</Select.Item
            >
          </Select.Content>
        </Select.Root>
      </div>
    </div>

    <!-- media grid -->
    <MediaGrid
      data={mediaData}
      {mediaType}
      {loading}
      {error}
      onViewDetails={handleViewDetails}
      onRequestException={handleRequestException}
      onPageChange={handlePageChange}
    />
  </div>
</div>

<!-- dialogs -->
<MediaDetailDialog
  bind:open={showDetailDialog}
  media={selectedMedia}
  {mediaType}
  onRequestException={handleRequestException}
/>

<ExceptionRequestDialog
  bind:open={showExceptionDialog}
  media={selectedMedia}
  {mediaType}
  onSuccess={handleExceptionSuccess}
/>
