<script lang="ts">
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import Star from "@lucide/svelte/icons/star";
  import Clock from "@lucide/svelte/icons/clock";
  import Calendar from "@lucide/svelte/icons/calendar";
  import ListX from "@lucide/svelte/icons/list-x";
  import Ticket from "@lucide/svelte/icons/ticket";
  import TicketMinus from "@lucide/svelte/icons/ticket-minus";
  import ArrowDownToLine from "@lucide/svelte/icons/arrow-down-to-line";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import { auth } from "$lib/stores/auth";
  import type {
    MediaItem,
    MediaType,
    MovieWithStatus,
    SeriesWithStatus,
  } from "$lib/types/shared";
  import { Permission } from "$lib/types/shared";
  import { formatSizeToGB, formatRuntime } from "$lib/utils/formatters";
  import { formatDateToLocaleString } from "$lib/utils/date";

  const TMDB_POSTER_WIDTH = 342;
  const TMDB_BACKDROP_WIDTH = 780;

  interface Props {
    open: boolean;
    media: MediaItem | null;
    mediaType: MediaType;
    onClose?: () => void;
    onRequestException?: (media: MediaItem) => void;
  }

  let {
    open = $bindable(),
    media,
    mediaType,
    onClose,
    onRequestException,
  }: Props = $props();

  const handleRequestException = () => {
    if (media && onRequestException) {
      onRequestException(media);
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
</script>

<Dialog.Root bind:open onOpenChange={(isOpen) => !isOpen && handleClose()}>
  <Dialog.Content
    showCloseButton={false}
    class="media-dialog sm:max-w-175 max-h-[90vh] overflow-hidden border-ring border-2 p-0"
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
          <div class="media-dialog__header">
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
                    <h2 class="text-2xl font-bold text-foreground mb-1">
                      {media.title}
                    </h2>
                    {#if media.original_title && media.original_title !== media.title}
                      <p class="text-sm text-muted-foreground">
                        {media.original_title}
                      </p>
                    {/if}
                  </div>

                  <!-- status indicators (top right) -->
                  <div class="absolute top-2 right-2 flex gap-1">
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
                                <TicketMinus
                                  class="size-5 text-white cursor-help"
                                />
                              </span>
                            {/snippet}
                          </Tooltip.Trigger>
                          <Tooltip.Content>
                            <p>Reclaim candidate</p>
                          </Tooltip.Content>
                        </Tooltip.Root>
                      </div>
                    {/if}

                    <!-- pending request -->
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
                            <p>Pending Request</p>
                          </Tooltip.Content>
                        </Tooltip.Root>
                      </div>
                    {/if}
                  </div>
                </div>

                <!-- tagline -->
                {#if media.tagline}
                  <p class="text-sm italic text-muted-foreground mb-3">
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
                  {#if media.vote_average}
                    <div class="flex items-center gap-1">
                      <Star class="w-4 h-4 fill-yellow-500 text-yellow-500" />
                      <span>{media.vote_average.toFixed(1)}/10</span>
                      {#if media.vote_count}
                        <span class="text-xs">({media.vote_count} votes)</span>
                      {/if}
                    </div>
                  {/if}
                </div>
              </div>

              <!-- genres -->
              {#if media.genres && media.genres.length > 0}
                <div class="flex flex-wrap gap-2">
                  {#each media.genres as genre}
                    <Badge
                      class="bg-gray-950/10 text-foreground border-muted-foreground"
                      variant="outline">{genre}</Badge
                    >
                  {/each}
                </div>
              {/if}
            </div>
          </div>

          <div class="space-y-4 flex-1 overflow-y-auto min-h-0">
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
              <div class="grid grid-cols-2 gap-2 text-sm text-foreground/80">
                <div>
                  <span class="text-muted-foreground">Size:</span>
                  <span class="text-foreground ml-2"
                    >{formatSizeToGB(media.size)}</span
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

          <Dialog.Footer class="mt-6 border-t border-border/60 pt-4">
            <Button
              variant="secondary"
              class="bg-primary hover:bg-primary-hover cursor-pointer"
              onclick={handleClose}>Close</Button
            >
            {#if canRequestExceptions && !media.status.is_protected && !media.status.has_pending_request}
              <Button class="cursor-pointer" onclick={handleRequestException}
                ><ArrowDownToLine class="size-5" />Request</Button
              >
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
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
    min-height: 0;
    width: 100%;
    backdrop-filter: blur(4px);
  }

  .media-dialog__header {
    display: flex;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }

  .media-dialog__poster {
    width: 120px;
    border-radius: 0.75rem;
    border: 1px solid color-mix(in oklch, var(--color-border) 70%, transparent);
    box-shadow: 0 12px 24px color-mix(in oklch, black 25%, transparent);
    flex-shrink: 0;
  }

  .media-dialog__header-content {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    min-width: 0;
  }

  @media (min-width: 640px) {
    .media-dialog__panel {
      padding: 2rem;
    }

    .media-dialog__poster {
      width: 140px;
    }
  }

  @media (max-width: 640px) {
    .media-dialog__header {
      flex-direction: column;
      align-items: center;
      text-align: center;
    }

    .media-dialog__header-content {
      align-items: center;
    }
  }
</style>
