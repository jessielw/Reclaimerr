<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import FolderOutput from "@lucide/svelte/icons/folder-output";
  import Info from "@lucide/svelte/icons/info";
  import Shield from "@lucide/svelte/icons/shield";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import { type ReclaimCandidateEntry } from "$lib/types/shared";

  interface Props {
    entry: ReclaimCandidateEntry;
    canDelete: boolean;
    moveEnabled: boolean;
    openSingleRequest: (entry: ReclaimCandidateEntry) => void;
    openSingleDelete: (entry: ReclaimCandidateEntry) => void;
    openSingleMove: (entry: ReclaimCandidateEntry) => void;
    // provide to show an Info button
    onInfo?: (entry: ReclaimCandidateEntry) => void;
    // wrap each button in a Tooltip (for desktop)
    showTooltips?: boolean;
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
    showTooltips = false,
    compact = false,
  }: Props = $props();

  const btnBase = $derived(
    compact
      ? "cursor-pointer rounded-full size-7 flex items-center justify-center"
      : "cursor-pointer rounded-full",
  );
  const iconCls = $derived(compact ? "size-3.5 shrink-0" : "size-4 shrink-0");
  const canMove = $derived(moveEnabled);
</script>

{#if entry.has_pending_request}
  <span class="text-xs text-blue-400 self-center">Pending request</span>
{:else if showTooltips}
  <Tooltip.Root>
    <Tooltip.Trigger>
      <Button
        size="icon"
        class="{btnBase} bg-green-600/80 hover:bg-green-600/60"
        onclick={() => openSingleRequest(entry)}
      >
        <Shield class={iconCls} />
      </Button>
    </Tooltip.Trigger>
    <Tooltip.Content><p>Protect</p></Tooltip.Content>
  </Tooltip.Root>
{:else}
  <Button
    size="icon"
    class="{btnBase} bg-green-600/80 hover:bg-green-600/60"
    onclick={() => openSingleRequest(entry)}
  >
    <Shield class={iconCls} />
  </Button>
{/if}

<!-- deletion permissions -->
{#if canDelete}
  {#if showTooltips}
    <Tooltip.Root>
      {#if canMove}
        <Tooltip.Root>
          <Tooltip.Trigger>
            <Button
              size="icon"
              class="{btnBase} bg-amber-500/80 hover:bg-amber-500/60"
              onclick={() => openSingleMove(entry)}
            >
              <FolderOutput class={iconCls} />
            </Button>
          </Tooltip.Trigger>
          <Tooltip.Content><p>Move to destination</p></Tooltip.Content>
        </Tooltip.Root>
      {/if}
      <Tooltip.Trigger>
        <Button
          size="icon"
          class="{btnBase} bg-destructive/80 hover:bg-destructive/60"
          onclick={() => openSingleDelete(entry)}
        >
          <Trash2 class={iconCls} />
        </Button>
      </Tooltip.Trigger>
      <Tooltip.Content><p>Delete</p></Tooltip.Content>
    </Tooltip.Root>
  {:else}
    {#if canMove}
      <Button
        size="icon"
        class="{btnBase} bg-amber-500/80 hover:bg-amber-500/60"
        onclick={() => openSingleMove(entry)}
      >
        <FolderOutput class={iconCls} />
      </Button>
    {/if}
    <Button
      size="icon"
      class="{btnBase} bg-destructive/80 hover:bg-destructive/60"
      onclick={() => openSingleDelete(entry)}
    >
      <Trash2 class={iconCls} />
    </Button>
  {/if}
{/if}

<!-- info -->
{#if onInfo}
  <div class="w-px bg-border self-stretch"></div>
  {#if showTooltips}
    <Tooltip.Root>
      <Tooltip.Trigger>
        <Button size="icon" class={btnBase} onclick={() => onInfo!(entry)}>
          <Info class={iconCls} />
        </Button>
      </Tooltip.Trigger>
      <Tooltip.Content><p>Details</p></Tooltip.Content>
    </Tooltip.Root>
  {:else}
    <Button size="icon" class={btnBase} onclick={() => onInfo!(entry)}>
      <Info class={iconCls} />
    </Button>
  {/if}
{/if}
