<script lang="ts">
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import Clock from "@lucide/svelte/icons/clock";
  import Calendar from "@lucide/svelte/icons/calendar";
  import ListX from "@lucide/svelte/icons/list-x";
  import Ticket from "@lucide/svelte/icons/ticket";
  import Shield from "@lucide/svelte/icons/shield";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import X from "@lucide/svelte/icons/x";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import { auth } from "$lib/stores/auth";
  import type {
    MediaItem,
    MediaType,
    MovieWithStatus,
    SeriesWithStatus,
  } from "$lib/types/shared";
  import MetadataSources from "$lib/components/media/metadata-sources.svelte";
  import { Permission } from "$lib/types/shared";
  import { formatFileSize, formatRuntime } from "$lib/utils/formatters";
  import { formatDateToLocaleString } from "$lib/utils/date";

  const TMDB_POSTER_WIDTH = 342;
  const TMDB_BACKDROP_WIDTH = 780;

  interface Props {
    open: boolean;
    media: MediaItem | null;
    mediaType: MediaType;
    onClose?: () => void;
    onRequestException?: (media: MediaItem) => void;
    onRequestDelete?: (media: MediaItem) => void;
  }

  let {
    open = $bindable(),
    media,
    mediaType,
    onClose,
    onRequestException,
    onRequestDelete,
  }: Props = $props();

  const handleRequestException = () => {
    if (media && onRequestException) {
      onRequestException(media);
      open = false;
    }
  };

  const handleRequestDelete = () => {
    if (media && onRequestDelete) {
      onRequestDelete(media);
      open = false;
    }
  };

  const handleClose = () => {
    open = false;
    if (onClose) {
      onClose();
    }
  };

  const isMovie = (media: MediaItem): media is MovieWithStatus => {
    return "runtime" in media;
  };

  const isSeries = (media: MediaItem): media is SeriesWithStatus => {
    return "season_count" in media;
  };

  const canRequestExceptions = $derived(
    $auth.user?.role === "admin" ||
      ($auth.user?.permissions ?? []).includes(Permission.Request) ||
      ($auth.user?.permissions ?? []).includes(Permission.AutoApprove),
  );
  const canRequestProtection = $derived(
    canRequestExceptions &&
      !!media &&
      !media.status.is_protected &&
      !media.status.has_pending_request,
  );
  const canRequestDelete = $derived(
    canRequestExceptions &&
      !!media &&
      !media.status.is_protected &&
      !media.status.has_pending_delete_request,
  );

  const tmdbMediaType = $derived(mediaType === "series" ? "tv" : "movie");
</script>

<Dialog.Root bind:open onOpenChange={(isOpen) => !isOpen && handleClose()}>
  <Dialog.Content
    showCloseButton={false}
    class="sm:max-w-175 h-[90vh] overflow-hidden border-ring border-2 p-0"
  >
    {#if media}
      <div class="media-dialog__body">
        {#if media.backdrop_url}
          <div
            class="media-dialog__backdrop"
            style="--backdrop-image: url('http://image.tmdb.org/t/p/w{TMDB_BACKDROP_WIDTH}/{media.backdrop_url}');"
          ></div>
        {/if}

        <div class="media-dialog__panel flex flex-col">
          <div class="sm:hidden media-dialog__mobile-hero">
            {#if media.poster_url}
              <img
                class="media-dialog__mobile-hero-poster"
                src="http://image.tmdb.org/t/p/w{TMDB_POSTER_WIDTH}/{media.poster_url}"
                alt="{media.title} poster"
                loading="lazy"
              />
            {:else}
              <div class="media-dialog__mobile-hero-fallback"></div>
            {/if}
            <div class="media-dialog__mobile-hero-overlay">
              <h2 class="text-lg font-bold text-white line-clamp-2">
                {media.title}
              </h2>
              <div
                class="flex flex-wrap gap-x-3 gap-y-1 text-xs text-white/90 mt-1"
              >
                <div class="flex items-center gap-1">
                  <Calendar class="w-3.5 h-3.5" />
                  <span>{media.year ?? "Unknown"}</span>
                </div>
                {#if isMovie(media) && media.runtime}
                  <div class="flex items-center gap-1">
                    <Clock class="w-3.5 h-3.5" />
                    <span>{formatRuntime(media.runtime)}</span>
                  </div>
                {/if}
                {#if isSeries(media) && media.season_count}
                  <div class="flex items-center gap-1">
                    <Clock class="w-3.5 h-3.5" />
                    <span>{media.season_count} Seasons</span>
                  </div>
                {/if}
              </div>
              {#if media.status.is_protected || media.status.is_candidate || media.status.has_pending_request || media.status.has_pending_delete_request}
                <div class="flex flex-wrap gap-1.5 mt-2">
                  {#if media.status.is_protected}
                    <Tooltip.Root>
                      <Tooltip.Trigger>
                        {#snippet child({ props })}
                          <span
                            {...props}
                            tabindex="-1"
                            class="inline-flex h-6 min-w-6 rounded-full bg-gray-500 items-center justify-center px-1.5"
                          >
                            <ListX class="size-3.5 text-white cursor-help" />
                          </span>
                        {/snippet}
                      </Tooltip.Trigger>
                      <Tooltip.Content>
                        <p>Protected</p>
                      </Tooltip.Content>
                    </Tooltip.Root>
                  {/if}
                  {#if media.status.is_candidate}
                    <Tooltip.Root>
                      <Tooltip.Trigger>
                        {#snippet child({ props })}
                          <span
                            {...props}
                            tabindex="-1"
                            class="inline-flex h-6 min-w-6 rounded-full bg-yellow-500 items-center justify-center px-1.5"
                          >
                            <Trash2 class="size-3.5 text-white cursor-help" />
                          </span>
                        {/snippet}
                      </Tooltip.Trigger>
                      <Tooltip.Content>
                        <p>Reclaim candidate</p>
                      </Tooltip.Content>
                    </Tooltip.Root>
                  {/if}
                  {#if media.status.has_pending_request}
                    <Tooltip.Root>
                      <Tooltip.Trigger>
                        {#snippet child({ props })}
                          <span
                            {...props}
                            tabindex="-1"
                            class="inline-flex h-6 min-w-6 rounded-full bg-blue-500 items-center justify-center px-1.5"
                          >
                            <Ticket class="size-3.5 text-white cursor-help" />
                          </span>
                        {/snippet}
                      </Tooltip.Trigger>
                      <Tooltip.Content>
                        <p>Pending Protection Request</p>
                      </Tooltip.Content>
                    </Tooltip.Root>
                  {/if}
                  {#if media.status.has_pending_delete_request}
                    <Tooltip.Root>
                      <Tooltip.Trigger>
                        {#snippet child({ props })}
                          <span
                            {...props}
                            tabindex="-1"
                            class="inline-flex h-6 min-w-6 rounded-full bg-red-500 items-center justify-center px-1.5"
                          >
                            <Trash2 class="size-3.5 text-white cursor-help" />
                          </span>
                        {/snippet}
                      </Tooltip.Trigger>
                      <Tooltip.Content>
                        <p>Pending Delete Request</p>
                      </Tooltip.Content>
                    </Tooltip.Root>
                  {/if}
                </div>
              {/if}
            </div>
          </div>

          <div class="hidden sm:flex media-dialog__header">
            {#if media.poster_url}
              <img
                class="media-dialog__poster"
                src="http://image.tmdb.org/t/p/w{TMDB_POSTER_WIDTH}/{media.poster_url}"
                alt="{media.title} poster"
                loading="lazy"
              />
            {/if}

            <div class="media-dialog__header-content">
              <!-- title and status badges -->
              <div>
                <div class="flex items-start justify-between gap-4 mb-2">
                  <div class="flex-1">
                    <h2
                      class="text-xl sm:text-2xl font-bold text-foreground mb-1 line-clamp-2"
                    >
                      {media.title}
                    </h2>
                    {#if media.original_title && media.original_title !== media.title}
                      <p class="hidden sm:block text-sm text-muted-foreground">
                        {media.original_title}
                      </p>
                    {/if}
                  </div>

                  <!-- status indicators (top right) -->
                  <div class="hidden sm:flex absolute top-2 right-2 gap-1">
                    <!-- protected -->
                    {#if media.status.is_protected}
                      <div
                        class="w-7 h-7 rounded-full bg-gray-500 flex items-center justify-center z-40"
                      >
                        <Tooltip.Root>
                          <Tooltip.Trigger>
                            {#snippet child({ props })}
                              <span {...props} tabindex="-1">
                                <ListX class="size-5 text-white cursor-help" />
                              </span>
                            {/snippet}
                          </Tooltip.Trigger>
                          <Tooltip.Content>
                            <p>Protected</p>
                          </Tooltip.Content>
                        </Tooltip.Root>
                      </div>
                    {/if}

                    <!-- deletion candidate -->
                    {#if media.status.is_candidate}
                      <div
                        class="w-7 h-7 rounded-full bg-yellow-500 flex items-center justify-center z-40"
                      >
                        <Tooltip.Root>
                          <Tooltip.Trigger>
                            {#snippet child({ props })}
                              <span {...props} tabindex="-1">
                                <Trash2 class="size-5 text-white cursor-help" />
                              </span>
                            {/snippet}
                          </Tooltip.Trigger>
                          <Tooltip.Content>
                            <p>Reclaim candidate</p>
                          </Tooltip.Content>
                        </Tooltip.Root>
                      </div>
                    {/if}

                    <!-- pending protection request -->
                    {#if media.status.has_pending_request}
                      <div
                        class="w-7 h-7 rounded-full bg-blue-500 flex items-center justify-center z-40"
                      >
                        <Tooltip.Root>
                          <Tooltip.Trigger>
                            {#snippet child({ props })}
                              <span {...props} tabindex="-1">
                                <Ticket class="size-5 text-white cursor-help" />
                              </span>
                            {/snippet}
                          </Tooltip.Trigger>
                          <Tooltip.Content>
                            <p>Pending Protection Request</p>
                          </Tooltip.Content>
                        </Tooltip.Root>
                      </div>
                    {/if}

                    <!-- pending delete request -->
                    {#if media.status.has_pending_delete_request}
                      <div
                        class="w-7 h-7 rounded-full bg-red-500 flex items-center justify-center z-40"
                      >
                        <Tooltip.Root>
                          <Tooltip.Trigger>
                            {#snippet child({ props })}
                              <span {...props} tabindex="-1">
                                <Trash2 class="size-5 text-white cursor-help" />
                              </span>
                            {/snippet}
                          </Tooltip.Trigger>
                          <Tooltip.Content>
                            <p>Pending Delete Request</p>
                          </Tooltip.Content>
                        </Tooltip.Root>
                      </div>
                    {/if}
                  </div>
                </div>

                <!-- tagline -->
                {#if media.tagline}
                  <p
                    class="hidden sm:block text-sm italic text-muted-foreground mb-3"
                  >
                    "{media.tagline}"
                  </p>
                {/if}

                <!-- meta info -->
                <div class="flex flex-wrap gap-3 text-sm text-muted-foreground">
                  <div class="flex items-center gap-1">
                    <Calendar class="w-4 h-4" />
                    <span>{media.year ?? "Unknown"}</span>
                  </div>
                  {#if isMovie(media) && media.runtime}
                    <div class="flex items-center gap-1">
                      <Clock class="w-4 h-4" />
                      <span>{formatRuntime(media.runtime)}</span>
                    </div>
                  {/if}
                  {#if isSeries(media) && media.season_count}
                    <div class="flex items-center gap-1">
                      <Clock class="w-4 h-4" />
                      <span>{media.season_count} Seasons</span>
                    </div>
                  {/if}
                </div>

                {#if media.vote_average != null || media.vote_count != null || media.imdb_rating != null || media.imdb_vote_count != null || media.anilist_score != null || media.anilist_popularity != null || media.anilist_favourites != null}
                  <MetadataSources
                    tmdbId={media.tmdb_id}
                    {tmdbMediaType}
                    tmdbRating={media.vote_average}
                    tmdbVoteCount={media.vote_count}
                    imdbId={media.imdb_id}
                    imdbRating={media.imdb_rating}
                    imdbVoteCount={media.imdb_vote_count}
                    anilistId={media.anilist_id}
                    anilistScore={media.anilist_score}
                    anilistPopularity={media.anilist_popularity}
                    anilistFavourites={media.anilist_favourites}
                    class="mt-1 sm:mt-2"
                  />
                {/if}
              </div>

              <!-- genres -->
              {#if media.genres && media.genres.length > 0}
                <div
                  class="flex gap-1 sm:gap-2 overflow-x-auto sm:overflow-visible whitespace-nowrap sm:whitespace-normal pb-0.5"
                >
                  {#each media.genres as genre, i}
                    <Badge
                      class="shrink-0 bg-gray-950/10 text-foreground border-muted-foreground {i >
                      2
                        ? 'hidden sm:inline-flex'
                        : ''}"
                      variant="outline">{genre}</Badge
                    >
                  {/each}
                  {#if media.genres.length > 3}
                    <Badge
                      class="shrink-0 sm:hidden bg-gray-950/10 text-foreground border-muted-foreground"
                      variant="outline"
                    >
                      +{media.genres.length - 3}
                    </Badge>
                  {/if}
                </div>
              {/if}
            </div>
          </div>

          <div class="sm:hidden mb-2">
            {#if media.vote_average != null || media.vote_count != null || media.imdb_rating != null || media.imdb_vote_count != null || media.anilist_score != null || media.anilist_popularity != null || media.anilist_favourites != null}
              <MetadataSources
                tmdbId={media.tmdb_id}
                {tmdbMediaType}
                tmdbRating={media.vote_average}
                tmdbVoteCount={media.vote_count}
                imdbId={media.imdb_id}
                imdbRating={media.imdb_rating}
                imdbVoteCount={media.imdb_vote_count}
                anilistId={media.anilist_id}
                anilistScore={media.anilist_score}
                anilistPopularity={media.anilist_popularity}
                anilistFavourites={media.anilist_favourites}
                class="mt-1"
              />
            {/if}
            {#if media.genres && media.genres.length > 0}
              <div
                class="flex gap-1 overflow-x-auto whitespace-nowrap pb-0.5 mt-2"
              >
                {#each media.genres as genre, i}
                  <Badge
                    class="shrink-0 bg-gray-950/10 text-foreground border-muted-foreground {i >
                    2
                      ? 'hidden'
                      : ''}"
                    variant="outline">{genre}</Badge
                  >
                {/each}
                {#if media.genres.length > 3}
                  <Badge
                    class="shrink-0 bg-gray-950/10 text-foreground border-muted-foreground"
                    variant="outline"
                  >
                    +{media.genres.length - 3}
                  </Badge>
                {/if}
              </div>
            {/if}
          </div>

          <div class="space-y-3 sm:space-y-4 flex-1 overflow-y-auto min-h-0">
            <!-- overview -->
            {#if media.overview}
              <div>
                <h3 class="text-lg font-semibold text-foreground mb-2">
                  Overview
                </h3>
                <p class="text-foreground/90 leading-relaxed">
                  {media.overview}
                </p>
              </div>
            {/if}

            <!-- file and library info -->
            <div class="space-y-2">
              <h3 class="text-lg font-semibold text-foreground mb-3">
                File Information
              </h3>
              <div
                class="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm text-foreground/80"
              >
                <div>
                  <span class="text-muted-foreground">Size:</span>
                  <span class="text-foreground ml-2"
                    >{formatFileSize(media.size)}</span
                  >
                </div>
                <div>
                  <span class="text-muted-foreground">Library:</span>
                  <span class="text-foreground ml-2">
                    {isSeries(media) && media.service_refs.length > 0
                      ? media.service_refs.map((r) => r.library_name).join(", ")
                      : isMovie(media) && media.versions.length > 0
                        ? [
                            ...new Set(
                              media.versions.map((v) => v.library_name),
                            ),
                          ].join(", ")
                        : "Unknown"}
                  </span>
                </div>
                <div>
                  <span class="text-muted-foreground">Watch Count:</span>
                  <span class="text-foreground ml-2">
                    {!media.last_viewed_at
                      ? "Never watched"
                      : `${media.view_count} views`}
                  </span>
                </div>
                {#if isMovie(media) && media.tmdb_in_collection && media.tmdb_collection_name}
                  <div>
                    <span class="text-muted-foreground">Collection:</span>
                    <span class="text-foreground ml-2"
                      >{media.tmdb_collection_name}</span
                    >
                  </div>
                {/if}
                {#if media.last_viewed_at}
                  <div>
                    <span class="text-muted-foreground">Last Viewed:</span>
                    <span class="text-foreground ml-2"
                      >{formatDateToLocaleString(media.last_viewed_at)}</span
                    >
                  </div>
                {/if}
                {#if media.added_at}
                  <div>
                    <span class="text-muted-foreground">Added:</span>
                    <span class="text-foreground ml-2"
                      >{formatDateToLocaleString(media.added_at)}</span
                    >
                  </div>
                {/if}
              </div>
            </div>
          </div>

          <div
            class="sm:hidden mt-3 sticky bottom-0 border-t border-border/40 pt-2"
          >
            {#if canRequestProtection || canRequestDelete}
              <div
                class="grid gap-2 {canRequestProtection && canRequestDelete
                  ? 'grid-cols-2'
                  : 'grid-cols-1'}"
              >
                {#if canRequestProtection}
                  <Button
                    class="cursor-pointer w-full"
                    onclick={handleRequestException}
                  >
                    <Shield class="size-5" />
                    Protect
                  </Button>
                {/if}
                {#if canRequestDelete}
                  <Button
                    variant="destructive"
                    class="cursor-pointer w-full"
                    onclick={handleRequestDelete}
                  >
                    <Trash2 class="size-5" />
                    Delete
                  </Button>
                {/if}
              </div>
            {/if}
            <Button
              variant="secondary"
              class="cursor-pointer w-full mt-2"
              onclick={handleClose}
            >
              <X class="size-5" />
              Close
            </Button>
          </div>

          <Dialog.Footer
            class="hidden sm:flex mt-6 border-t border-border/60 pt-4"
          >
            <!-- close button -->
            <Button
              variant="secondary"
              class="cursor-pointer"
              onclick={handleClose}
            >
              <X class="size-5" />
              Close
            </Button>

            <!-- request protection -->
            {#if canRequestProtection}
              <Tooltip.Root>
                <Tooltip.Trigger>
                  <Button
                    class="cursor-pointer"
                    onclick={handleRequestException}
                    ><Shield class="size-5" />Protect</Button
                  >
                </Tooltip.Trigger>
                <Tooltip.Content>
                  <p>Request Protection</p>
                </Tooltip.Content>
              </Tooltip.Root>
            {/if}

            <!-- request delete -->
            {#if canRequestDelete}
              <Tooltip.Root>
                <Tooltip.Trigger>
                  <Button
                    variant="destructive"
                    class="cursor-pointer"
                    onclick={handleRequestDelete}
                    ><Trash2 class="size-5" />Delete</Button
                  >
                </Tooltip.Trigger>
                <Tooltip.Content>
                  <p>Request Deletion</p>
                </Tooltip.Content>
              </Tooltip.Root>
            {/if}
          </Dialog.Footer>
        </div>
      </div>
    {/if}
  </Dialog.Content>
</Dialog.Root>

<style>
  .media-dialog__backdrop {
    position: absolute;
    inset: 0;
    background-image:
      linear-gradient(
        to bottom,
        color-mix(in oklch, black 25%, transparent),
        color-mix(in oklch, black 35%, transparent) 45%,
        color-mix(in oklch, black 50%, transparent) 100%
      ),
      var(--backdrop-image);
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
  }

  .media-dialog__body {
    position: relative;
    max-height: 90vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .media-dialog__panel {
    position: relative;
    z-index: 1;
    background: color-mix(in oklch, var(--color-card) 70%, transparent);
    border: 0;
    color: var(--color-card-foreground);
    padding: 1rem;
    overflow: hidden;
    flex: 1;
    min-height: 0;
    width: 100%;
    backdrop-filter: blur(4px);
  }

  .media-dialog__header {
    align-items: flex-start;
    gap: 0.75rem;
    margin-bottom: 1rem;
  }

  .media-dialog__mobile-hero {
    position: relative;
    margin-bottom: 0.75rem;
    border-radius: 0.75rem;
    overflow: hidden;
    border: 1px solid color-mix(in oklch, var(--color-border) 70%, transparent);
    box-shadow: 0 12px 24px color-mix(in oklch, black 25%, transparent);
  }

  .media-dialog__mobile-hero-poster {
    width: 100%;
    aspect-ratio: 16 / 9;
    object-fit: cover;
    display: block;
  }

  .media-dialog__mobile-hero-fallback {
    width: 100%;
    aspect-ratio: 16 / 9;
    background: color-mix(in oklch, var(--color-card) 80%, black);
  }

  .media-dialog__mobile-hero-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    padding: 0.75rem;
    background: linear-gradient(
      to top,
      color-mix(in oklch, black 72%, transparent) 8%,
      color-mix(in oklch, black 35%, transparent) 55%,
      color-mix(in oklch, black 10%, transparent) 100%
    );
  }

  .media-dialog__poster {
    width: 76px;
    border-radius: 0.5rem;
    border: 1px solid color-mix(in oklch, var(--color-border) 70%, transparent);
    box-shadow: 0 12px 24px color-mix(in oklch, black 25%, transparent);
    flex-shrink: 0;
  }

  .media-dialog__header-content {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    min-width: 0;
  }

  @media (min-width: 640px) {
    .media-dialog__panel {
      padding: 2rem;
    }

    .media-dialog__poster {
      width: 140px;
      border-radius: 0.75rem;
    }
  }
</style>
