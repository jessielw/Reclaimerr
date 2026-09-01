<script lang="ts">
  import type { Component } from "svelte";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import BadgeCheck from "@lucide/svelte/icons/badge-check";
  import Pencil from "@lucide/svelte/icons/pencil";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import Star from "@lucide/svelte/icons/star";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import { formatDistanceToNow } from "$lib/utils/date";
  import type { MediaServerState } from "./types";

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
  }: Props = $props();

  const isMain = $derived(server.config.isMain);
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
          <span class="font-medium text-foreground break-all"
            >{server.config.name}</span
          >
          {#if isMain}
            <Badge class="gap-1">
              <BadgeCheck class="size-3" />
              Main server
            </Badge>
          {:else}
            <Badge variant="secondary">Linked</Badge>
          {/if}
          {#if !server.config.enabled}
            <Badge variant="outline" class="text-muted-foreground"
              >Disabled</Badge
            >
          {/if}
        </div>
        <p class="text-xs text-muted-foreground break-all">
          {label} &middot; {server.config.baseUrl}
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
          class="cursor-pointer gap-2"
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
        class="cursor-pointer gap-2"
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
        class="cursor-pointer gap-2"
        disabled={busy}
        onclick={onEdit}
      >
        <Pencil class="size-4" />
        Edit
      </Button>
      {#if !isMain}
        <Button
          size="sm"
          class="cursor-pointer gap-2 bg-destructive/80 hover:bg-destructive text-destructive-foreground"
          disabled={busy || server.syncing}
          onclick={onDelete}
        >
          <Trash2 class="size-4" />
          Delete
        </Button>
      {/if}
    </div>
  </div>
</div>
