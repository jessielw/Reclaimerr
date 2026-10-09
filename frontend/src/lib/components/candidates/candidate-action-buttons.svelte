<script lang="ts">
  import type { Component } from "svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import FolderOutput from "@lucide/svelte/icons/folder-output";
  import Info from "@lucide/svelte/icons/info";
  import Shield from "@lucide/svelte/icons/shield";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import { MediaType, type ReclaimCandidateEntry } from "$lib/types/shared";

  interface Props {
    entry: ReclaimCandidateEntry;
    canDelete: boolean;
    moveEnabled: boolean;
    openSingleRequest: (entry: ReclaimCandidateEntry) => void;
    openSingleDelete: (entry: ReclaimCandidateEntry) => void;
    openSingleMove: (entry: ReclaimCandidateEntry) => void;
    // provide to show an Info button
    onInfo?: (entry: ReclaimCandidateEntry) => void;
    // smaller button/icon size for sub rows
    compact?: boolean;
  }

  let {
    entry,
    canDelete,
    moveEnabled,
    openSingleRequest,
    openSingleDelete,
    openSingleMove,
    onInfo,
    compact = false,
  }: Props = $props();

  const btnBase = $derived(
    compact
      ? "cursor-pointer rounded-full size-7 flex items-center justify-center"
      : "cursor-pointer rounded-full",
  );
  const iconCls = $derived(compact ? "size-3.5 shrink-0" : "size-4 shrink-0");

  // what the buttons act on, so tooltips make the scope explicit
  const scope = $derived(
    entry.movie_version_id != null
      ? "version"
      : entry.episode_number != null
        ? "episode"
        : entry.season_id != null
          ? "season"
          : entry.media_type === MediaType.Movie
            ? "movie"
            : "series",
  );
</script>

{#snippet action(
  label: string,
  colorCls: string,
  Icon: Component<{ class?: string }>,
  onclick: () => void,
)}
  <Tooltip.Root>
    <Tooltip.Trigger>
      {#snippet child({ props })}
        <Button
          {...props}
          size="icon"
          class="{btnBase} {colorCls}"
          aria-label={label}
          {onclick}
        >
          <Icon class={iconCls} />
        </Button>
      {/snippet}
    </Tooltip.Trigger>
    <Tooltip.Content><p>{label}</p></Tooltip.Content>
  </Tooltip.Root>
{/snippet}

{#if entry.has_pending_request}
  <span class="text-xs text-blue-400 self-center">Pending request</span>
{:else}
  {@render action(
    `Protect ${scope}`,
    "bg-green-600/80 hover:bg-green-600/60",
    Shield,
    () => openSingleRequest(entry),
  )}
{/if}

<!-- deletion permissions -->
{#if canDelete}
  {#if moveEnabled}
    {@render action(
      `Move ${scope} to destination`,
      "bg-amber-500/80 hover:bg-amber-500/60",
      FolderOutput,
      () => openSingleMove(entry),
    )}
  {/if}
  {@render action(
    `Delete ${scope}`,
    "bg-destructive/80 hover:bg-destructive/60",
    Trash2,
    () => openSingleDelete(entry),
  )}
{/if}

<!-- info -->
{#if onInfo}
  <div class="w-px bg-border self-stretch"></div>
  {@render action("Details", "", Info, () => onInfo!(entry))}
{/if}
