<script lang="ts">
  import { onMount } from "svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import Info from "@lucide/svelte/icons/info";
  import ArrowDownToLine from "@lucide/svelte/icons/arrow-down-to-line";
  import Ticket from "@lucide/svelte/icons/ticket";
  import TicketMinus from "@lucide/svelte/icons/ticket-minus";
  import ShieldBan from "@lucide/svelte/icons/shield-ban";
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
    onViewDetails?: (media: MediaItem) => void;
  }

  let {
    media,
    mediaType,
    showMediaType = false,
    onRequestException,
    onViewDetails,
  }: Props = $props();

  let cardEl: HTMLDivElement;
  let cardWidth = $state(0);
  const REQUEST_TEXT_MIN_WIDTH = 160;

  let isHovered = $state(false);
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

  onMount(() => {
    const observer = new ResizeObserver(([entry]) => {
      cardWidth = entry.contentRect.width;
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
    class="relative aspect-2/3 bg-gray-800 rounded-lg overflow-hidden border-2 border-gray-400
      dark:border-gray-700 hover:scale-105"
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

    <!-- status indicators (top right) -->
    <div class="absolute top-2 right-2 flex gap-1">
      <!-- protected -->
      {#if media.status.is_protected}
        <div
          class="w-7 h-7 rounded-full bg-gray-500 flex items-center justify-center z-20"
        >
          <Tooltip.Root>
            <Tooltip.Trigger>
              <ShieldBan class="size-5 text-white cursor-help" />
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
          class="w-7 h-7 rounded-full bg-yellow-500 flex items-center justify-center z-20"
        >
          <Tooltip.Root>
            <Tooltip.Trigger>
              <TicketMinus class="size-5 text-white cursor-help" />
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
          class="w-7 h-7 rounded-full bg-blue-500 flex items-center justify-center z-20"
        >
          <Tooltip.Root>
            <Tooltip.Trigger>
              <Ticket class="size-5 text-white cursor-help" />
            </Tooltip.Trigger>
            <Tooltip.Content>
              <p>Pending Request</p>
            </Tooltip.Content>
          </Tooltip.Root>
        </div>
      {/if}
    </div>

    <!-- hover overlay -->
    {#if isHovered}
      {@const canRequest =
        canRequestExceptions &&
        !media.status.is_protected &&
        !media.status.has_pending_request}
      <div
        class="absolute inset-0 bg-linear-to-t from-black/50 via-black/75
          to-black/50 flex flex-col justify-end p-4 transition-opacity duration-200"
      >
        <!-- title and year -->
        <div class="text-left mb-3">
          <h3 class="text-base font-semibold text-white line-clamp-2 mb-1">
            {media.title}
          </h3>
          <p class="text-sm text-gray-300">{media.year}</p>
        </div>

        <!-- request button and info -->
        <div
          class="flex gap-0.5 z-20 {canRequest
            ? 'justify-center'
            : 'justify-end'}"
        >
          <!-- request -->
          {#if canRequest}
            <Button
              size="sm"
              class="cursor-pointer text-gray-200 bg-primary transition-colors rounded-tr-none 
                rounded-br-none hover:scale-103
              {cardWidth > REQUEST_TEXT_MIN_WIDTH ? 'flex-1' : ''}
              {!canRequest ? '' : 'rounded-tr-none rounded-br-none'}"
              onclick={handleRequestException}
            >
              <ArrowDownToLine class="size-5" />
              {#if cardWidth > REQUEST_TEXT_MIN_WIDTH}
                Request
              {/if}
            </Button>
          {/if}

          <!-- info -->
          <div>
            <Button
              size="sm"
              class="cursor-pointer text-gray-200 bg-primary transition-colors hover:scale-103
              {!canRequest ? '' : 'rounded-tl-none rounded-bl-none'}"
              onclick={handleInfoClick}
            >
              <Info class="size-6" />
            </Button>
          </div>
        </div>
      </div>
    {/if}
  </div>
</div>
