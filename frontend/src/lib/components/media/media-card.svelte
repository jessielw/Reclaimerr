<script lang="ts">
  import { onMount } from "svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as DropdownMenu from "$lib/components/ui/dropdown-menu/index.js";
  import Info from "@lucide/svelte/icons/info";
  import ArrowDownToLine from "@lucide/svelte/icons/arrow-down-to-line";
  import EllipsisVertical from "@lucide/svelte/icons/ellipsis-vertical";
  import Trash_2 from "@lucide/svelte/icons/trash-2";
  import Ticket from "@lucide/svelte/icons/ticket";
  import Shield from "@lucide/svelte/icons/shield";
  import FileImage from "@lucide/svelte/icons/file-image";
  import { auth } from "$lib/stores/auth";
  import {
    Permission,
    type MediaItem,
    type MediaType,
  } from "$lib/types/shared";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import Badge from "$lib/components/ui/badge/badge.svelte";

  const TMDB_POSTER_WIDTH = 500;

  interface Props {
    media: MediaItem;
    mediaType: MediaType;
    showMediaType?: boolean;
    onRequestException?: (media: MediaItem) => void;
    onRequestDelete?: (media: MediaItem) => void;
    onViewDetails?: (media: MediaItem) => void;
  }

  let {
    media,
    mediaType,
    showMediaType = false,
    onRequestException,
    onRequestDelete,
    onViewDetails,
  }: Props = $props();

  // border based on media type
  const borderColor = $derived.by(() => {
    const defaultColor = "bg-gray-800 dark:border-gray-700";
    if (!showMediaType) return defaultColor;
    if (mediaType === "movie") return "border-2 border-movie";
    if (mediaType === "series") return "border-2 border-series";
    return defaultColor;
  });

  // control buttons based on card size
  const REQUEST_TEXT_MIN_WIDTH = 145;
  let cardEl: HTMLDivElement;
  let cardWidth = $state(0);
  let badgeSize = $state("");
  let mediaCountSize = $state("text-xs");

  let isHovered = $state(false);
  let menuOpen = $state(false);
  const canRequestExceptions = $derived(
    $auth.user?.role === "admin" ||
      ($auth.user?.permissions ?? []).includes(Permission.Request) ||
      ($auth.user?.permissions ?? []).includes(Permission.AutoApprove),
  );

  const handleRequestException = (e: Event) => {
    e.stopPropagation();
    if (onRequestException) {
      onRequestException(media);
    }
  };

  const handleInfoClick = (e: Event) => {
    e.stopPropagation();
    if (onViewDetails) {
      onViewDetails(media);
    }
  };

  const handleRequestDelete = (e: Event) => {
    e.stopPropagation();
    if (onRequestDelete) {
      onRequestDelete(media);
    }
  };

  const seriesBadgeCount = $derived.by(() => {
    if (mediaType !== "series" || !("library_episode_count" in media)) return 0;
    return media.library_episode_count > 0
      ? media.library_episode_count
      : media.library_season_count;
  });

  const seriesBadgeTooltip = $derived.by(() => {
    if (mediaType !== "series" || !("library_episode_count" in media))
      return "";
    if (media.library_episode_count > 0) {
      return `Episodes (${media.library_episode_count}) • Seasons (${media.library_season_count})`;
    }
    return `Seasons (${media.library_season_count})`;
  });

  onMount(() => {
    const observer = new ResizeObserver(([entry]) => {
      cardWidth = entry.contentRect.width;
      if (cardWidth > REQUEST_TEXT_MIN_WIDTH) {
        badgeSize = "size-5";
        mediaCountSize = "text-sm";
      } else {
        badgeSize = "size-3";
        mediaCountSize = "text-xs";
      }
    });
    if (cardEl) observer.observe(cardEl);
    return () => observer.disconnect();
  });
</script>

<div
  class="group relative"
  bind:this={cardEl}
  onmouseenter={() => (isHovered = true)}
  onmouseleave={() => (isHovered = false)}
  role="contentinfo"
>
  <!-- main card -->
  <div
    class="relative aspect-2/3 rounded-lg overflow-hidden border-2 transition-transform
      duration-150 hover:scale-105 {borderColor}"
  >
    <!-- poster image -->
    {#if media.poster_url}
      <img
        src={`http://image.tmdb.org/t/p/w${TMDB_POSTER_WIDTH}/${media.poster_url}`}
        alt={media.title}
        class="w-full h-full object-cover"
      />
    {:else}
      <div class="w-full h-full flex items-center justify-center bg-gray-800">
        <FileImage class="size-16 text-gray-500" />
      </div>
    {/if}

    <!-- media type badge (top left) -->
    {#if showMediaType}
      <div class="absolute top-2 left-2">
        <Badge
          class="bg-tmdb/90 text-white text-xs font-semibold backdrop-blur-sm uppercase"
        >
          {mediaType}
        </Badge>
      </div>
    {/if}

    <!-- count badge (top left, below media type if shown) -->
    <div class="absolute left-2 top-2 z-20">
      <div class="flex flex-col gap-1 items-start z-20">
        {#if mediaType === "movie" && "versions" in media && media.versions.length > 1}
          <Tooltip.Root>
            <Tooltip.Trigger>
              <Badge
                class="bg-black/60 text-white font-semibold backdrop-blur-sm py-0.5 px-2 
                  rounded-md min-w-[1.6rem] flex items-center justify-center cursor-help"
              >
                <span class="{mediaCountSize} text-center"
                  >{media.versions.length}</span
                >
              </Badge>
            </Tooltip.Trigger>
            <Tooltip.Content>
              <p>Movie Versions ({media.versions.length})</p>
            </Tooltip.Content>
          </Tooltip.Root>
        {:else if mediaType === "series" && "library_season_count" in media && seriesBadgeCount > 0}
          <Tooltip.Root>
            <Tooltip.Trigger>
              <Badge
                class="bg-black/60 text-white font-semibold backdrop-blur-sm py-0.5 px-2 
                rounded-md min-w-[1.6rem] flex items-center justify-center cursor-help"
              >
                <span class="{mediaCountSize} text-center"
                  >{seriesBadgeCount}</span
                >
              </Badge>
            </Tooltip.Trigger>
            <Tooltip.Content>
              <p>{seriesBadgeTooltip}</p>
            </Tooltip.Content>
          </Tooltip.Root>
        {/if}
      </div>
    </div>

    <!-- status indicators (top right) -->
    <div class="absolute top-2 right-2 flex flex-col gap-1 items-end">
      <!-- protected -->
      {#if media.status.is_protected}
        <div
          class="rounded-full bg-gray-500 flex items-center justify-center z-20"
        >
          <Tooltip.Root>
            <Tooltip.Trigger>
              <Shield class="{badgeSize} text-white cursor-help m-1" />
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
          class="rounded-full bg-yellow-500 flex items-center justify-center z-20"
        >
          <Tooltip.Root>
            <Tooltip.Trigger>
              <Trash_2 class="{badgeSize} text-white cursor-help m-1" />
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
          class="rounded-full bg-blue-500 flex items-center justify-center z-20"
        >
          <Tooltip.Root>
            <Tooltip.Trigger>
              <Ticket class="{badgeSize} text-white cursor-help m-1" />
            </Tooltip.Trigger>
            <Tooltip.Content>
              <p>Pending Request</p>
            </Tooltip.Content>
          </Tooltip.Root>
        </div>
      {/if}

      {#if media.status.has_pending_delete_request}
        <div
          class="rounded-full bg-red-500 flex items-center justify-center z-20"
        >
          <Tooltip.Root>
            <Tooltip.Trigger>
              <Trash_2 class="{badgeSize} text-white cursor-help m-1" />
            </Tooltip.Trigger>
            <Tooltip.Content>
              <p>Pending Delete Request</p>
            </Tooltip.Content>
          </Tooltip.Root>
        </div>
      {/if}
    </div>

    <!-- hover overlay -->
    {#if isHovered || menuOpen}
      {@const canRequestProtection =
        canRequestExceptions &&
        !media.status.is_protected &&
        !media.status.has_pending_request}
      {@const canDeleteRequest =
        canRequestExceptions &&
        !media.status.is_protected &&
        !media.status.has_pending_delete_request}
      <div
        class="absolute inset-0 bg-linear-to-t from-black/50 via-black/75
          to-black/50 flex flex-col justify-end p-4 transition-opacity duration-200"
      >
        <!-- title and year -->
        <div class="text-left mb-3">
          <h3 class="text-base font-semibold text-white line-clamp-2 mb-1">
            {media.title}
          </h3>
          <p class="text-sm text-gray-300">{media.year ?? "Unknown"}</p>
        </div>

        <!-- action buttons (we move them to the right if the card is wide enough) -->
        <div
          class="flex gap-1.5 z-20 {cardWidth > REQUEST_TEXT_MIN_WIDTH
            ? 'justify-end'
            : 'justify-center'}"
        >
          <!-- actions dropdown (only when actions are available) -->
          {#if canRequestProtection || canDeleteRequest}
            <DropdownMenu.Root bind:open={menuOpen}>
              <DropdownMenu.Trigger>
                {#snippet child({ props })}
                  <Button
                    {...props}
                    size="icon-sm"
                    class="cursor-pointer text-gray-200 bg-primary/75 hover:bg-primary transition-colors"
                  >
                    <EllipsisVertical class="size-4" />
                  </Button>
                {/snippet}
              </DropdownMenu.Trigger>
              <DropdownMenu.Content align="end">
                {#if canRequestProtection}
                  <DropdownMenu.Item onclick={handleRequestException}>
                    <ArrowDownToLine class="size-4 mr-2" />
                    Request Protection
                  </DropdownMenu.Item>
                {/if}
                {#if canDeleteRequest}
                  <DropdownMenu.Item
                    variant="destructive"
                    onclick={handleRequestDelete}
                  >
                    <Trash_2 class="size-4 mr-2" />
                    Request Delete
                  </DropdownMenu.Item>
                {/if}
              </DropdownMenu.Content>
            </DropdownMenu.Root>
          {/if}

          <!-- info (always visible) -->
          <Button
            size="icon-sm"
            class="cursor-pointer text-gray-200 bg-primary/75 hover:bg-primary transition-colors"
            onclick={handleInfoClick}
          >
            <Info class="size-4" />
          </Button>
        </div>
      </div>
    {/if}
  </div>
</div>
