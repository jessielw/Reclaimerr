<script lang="ts">
  import { HISTORY_PROVIDER_LABELS } from "./types";
  import type { Component } from "svelte";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Checkbox } from "$lib/components/ui/checkbox/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import BadgeCheck from "@lucide/svelte/icons/badge-check";
  import Film from "@lucide/svelte/icons/film";
  import Folder from "@lucide/svelte/icons/folder";
  import Pencil from "@lucide/svelte/icons/pencil";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import Star from "@lucide/svelte/icons/star";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
  import Tv from "@lucide/svelte/icons/tv";
  import { formatDistanceToNow } from "$lib/utils/date";
  import type { MediaServerLibrary, MediaServerState } from "./types";

  interface Props {
    server: MediaServerState;
    label: string;
    icon: Component | null;
    /** any other action on the page is mid-flight */
    busy: boolean;
    onEdit: () => void;
    onSetMain: () => void;
    onSync: () => void;
    onDelete: () => void;
    onRefreshLibraries: () => void;
    onToggleLibrary: (library: MediaServerLibrary, selected: boolean) => void;
    onSetAllLibraries: (selected: boolean) => void;
  }

  let {
    server,
    label,
    icon,
    busy,
    onEdit,
    onSetMain,
    onSync,
    onDelete,
    onRefreshLibraries,
    onToggleLibrary,
    onSetAllLibraries,
  }: Props = $props();

  const isMain = $derived(server.config.isMain);
  const selectedCount = $derived(
    server.libraries.filter((lib) => lib.selected).length,
  );
  const allSelected = $derived(
    server.libraries.length > 0 && selectedCount === server.libraries.length,
  );
  const librariesBusy = $derived(
    busy || server.refreshingLibraries || server.savingLibraries,
  );
</script>

<div
  class="rounded-lg border p-4 {isMain
    ? 'border-primary/50 bg-primary/5'
    : 'border-border bg-background/40'}"
>
  <div class="flex flex-wrap items-start justify-between gap-4">
    <div class="flex min-w-0 items-start gap-3">
      <span
        class="flex size-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/40"
      >
        {#if icon}
          {@const Icon = icon}
          <Icon class="size-5" aria-hidden="true" />
        {/if}
      </span>
      <div class="min-w-0 space-y-1">
        <div class="flex flex-wrap items-center gap-2">
          <span class="font-medium text-foreground break-all">
            {server.config.name}
          </span>
          {#if isMain}
            <Badge class="gap-1">
              <BadgeCheck class="size-3" />
              Main server
            </Badge>
          {:else}
            <Badge variant="secondary">Linked</Badge>
          {/if}
          {#if !server.config.enabled}
            <Badge variant="outline" class="text-muted-foreground">
              Disabled
            </Badge>
          {/if}
        </div>
        <p class="text-xs text-muted-foreground break-all">
          {label} &middot; {server.config.baseUrl}
        </p>
        <p class="text-xs text-muted-foreground">
          History: {server.config.historySource?.error ??
            HISTORY_PROVIDER_LABELS[
              server.config.historySource?.provider ?? ""
            ] ??
            "not configured"}
        </p>
        <p class="text-xs text-muted-foreground">
          Last sync: {server.lastSyncedAt
            ? formatDistanceToNow(server.lastSyncedAt)
            : "never"}
        </p>
      </div>
    </div>

    <div class="flex flex-wrap items-center gap-2">
      {#if !isMain}
        <Button
          size="sm"
          variant="outline"
          class="gap-2"
          disabled={busy || server.syncing}
          title="Make this the source of library and media data"
          onclick={onSetMain}
        >
          <Star class="size-4" />
          Set as main
        </Button>
      {/if}
      <Button
        size="sm"
        variant="outline"
        class="gap-2"
        disabled={busy || server.syncing || !server.config.enabled}
        title={!server.config.enabled
          ? "Enable this server to sync it"
          : isMain
            ? "Run a full media sync from this server"
            : "Refresh watch data from this server"}
        onclick={onSync}
      >
        {#if server.syncing}
          <Spinner class="size-4" />
        {:else}
          <RefreshCw class="size-4" />
        {/if}
        Sync
      </Button>
      <Button
        size="sm"
        variant="outline"
        class="gap-2"
        disabled={busy}
        onclick={onEdit}
      >
        <Pencil class="size-4" />
        Edit
      </Button>
      {#if !isMain}
        <Button
          size="sm"
          class="gap-2 bg-destructive/80 hover:bg-destructive text-destructive-foreground"
          disabled={busy || server.syncing}
          onclick={onDelete}
        >
          <Trash2 class="size-4" />
          Delete
        </Button>
      {/if}
    </div>
  </div>

  {#if isMain}
    <div class="mt-4 border-t border-border/60 pt-3 space-y-2.5">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex flex-wrap items-center gap-2">
          <span
            class="flex items-center gap-1.5 text-sm font-medium text-foreground"
          >
            <Folder class="size-4 text-muted-foreground" />
            Synced libraries
          </span>
          {#if server.libraries.length > 0}
            <Badge
              variant={selectedCount === 0 ? "outline" : "secondary"}
              class="text-xs font-normal {selectedCount === 0
                ? 'border-warning-secondary text-warning-foreground'
                : ''}"
            >
              {selectedCount} of {server.libraries.length} selected
            </Badge>
          {/if}
        </div>
        <div class="flex flex-wrap items-center gap-2">
          {#if server.libraries.length > 1}
            <Button
              size="sm"
              variant="ghost"
              class="h-7 px-2 text-xs text-muted-foreground hover:text-foreground"
              disabled={librariesBusy}
              onclick={() => onSetAllLibraries(!allSelected)}
            >
              {allSelected ? "Deselect all" : "Select all"}
            </Button>
          {/if}
          <Button
            size="sm"
            variant="outline"
            class="h-7 gap-1.5 px-2.5 text-xs"
            disabled={librariesBusy || !server.config.enabled}
            title="Fetch the latest library list without running a full media sync"
            onclick={onRefreshLibraries}
          >
            {#if server.refreshingLibraries}
              <Spinner class="size-3.5" />
            {:else}
              <RefreshCw class="size-3.5" />
            {/if}
            Refresh libraries
          </Button>
        </div>
      </div>

      <p class="text-xs text-muted-foreground">
        Choose which libraries are included during <strong>Sync Media</strong>.
        Unselected libraries are skipped completely.
      </p>

      {#if server.libraries.length === 0}
        <div
          class="rounded-md border border-dashed border-border/80 bg-background/40 px-3 py-3 text-xs text-muted-foreground"
        >
          No libraries loaded yet. Click <strong>Refresh libraries</strong> to
          fetch libraries from {label} before running Sync Media.
        </div>
      {:else}
        <div
          class="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-72 overflow-y-auto pr-0.5"
        >
          {#each server.libraries as library (library.id)}
            <label
              class="flex items-center justify-between gap-2.5 rounded-md border px-3 py-2 text-sm transition-colors cursor-pointer {library.selected
                ? 'border-border bg-background/80 text-foreground hover:bg-background'
                : 'border-border/60 bg-muted/30 text-muted-foreground hover:bg-muted/50'} {librariesBusy
                ? 'opacity-60 pointer-events-none'
                : ''}"
            >
              <span class="flex min-w-0 items-center gap-2.5">
                <Checkbox
                  checked={library.selected}
                  disabled={librariesBusy}
                  onCheckedChange={(checked) =>
                    onToggleLibrary(library, checked === true)}
                />
                <span class="truncate font-medium" title={library.libraryName}>
                  {library.libraryName}
                </span>
              </span>
              <Badge
                variant="outline"
                class="shrink-0 gap-1 text-[11px] font-normal text-muted-foreground"
              >
                {#if library.mediaType === "movie"}
                  <Film class="size-3" />
                  Movies
                {:else}
                  <Tv class="size-3" />
                  Shows
                {/if}
              </Badge>
            </label>
          {/each}
        </div>

        {#if selectedCount === 0}
          <div
            class="flex items-start gap-2 rounded-md border border-warning-secondary bg-warning/30 px-3 py-2 text-xs text-warning-foreground"
          >
            <TriangleAlert class="size-3.5 mt-0.5 shrink-0" />
            <span>
              No libraries are selected. Running <strong>Sync Media</strong>
              will skip all movie and show libraries.
            </span>
          </div>
        {/if}
      {/if}
    </div>
  {/if}
</div>
