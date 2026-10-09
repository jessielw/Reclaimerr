<script lang="ts">
  import { onMount } from "svelte";
  import { get_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import Notice from "$lib/components/notice.svelte";
  import Download from "@lucide/svelte/icons/download";
  import { formatFileSize } from "$lib/utils/formatters";

  interface DatabaseBackupInfo {
    name: string;
    size_bytes: number;
    created_at: string;
  }

  interface Props {
    retention: number;
  }

  let { retention = $bindable() }: Props = $props();

  let backups = $state<DatabaseBackupInfo[]>([]);
  let error = $state<string | null>(null);

  onMount(async () => {
    try {
      backups = await get_api<DatabaseBackupInfo[]>("/api/settings/backups");
    } catch (e: any) {
      error = e.message ?? "Could not load backups.";
    }
  });
</script>

<div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
  <h3 class="font-semibold text-foreground mb-1">Database Backups</h3>
  <p class="text-muted-foreground text-sm mb-3">
    The <strong>Back Up Database</strong> task saves a copy of the database to
    the <code>backups</code> folder in the data directory every week. You can
    also download a copy now. To restore, stop Reclaimerr, replace
    <code>database/reclaimerr.db</code> with the backup, and start it again. A
    restore onto a new install also needs the original <code>secrets.env</code>.
  </p>

  <Notice type="warning" title="Keep backups private" class="mb-3">
    Backups contain service API keys, media server tokens, and email settings.
  </Notice>

  <div class="flex flex-wrap items-end gap-4 mb-3">
    <Button
      variant="outline"
      size="sm"
      class="cursor-pointer gap-2"
      href="/api/settings/backups/download"
      download
    >
      <Download class="size-4" />
      Download backup now
    </Button>
    <div class="space-y-2">
      <Label for="databaseBackupRetention" class="text-sm text-foreground">
        Scheduled backups to keep
      </Label>
      <Input
        id="databaseBackupRetention"
        type="number"
        min="1"
        max="100"
        step="1"
        class="w-32"
        bind:value={retention}
      />
    </div>
  </div>

  {#if error}
    <p class="text-sm text-destructive">{error}</p>
  {:else if backups.length === 0}
    <p class="text-sm text-muted-foreground">No scheduled backups yet.</p>
  {:else}
    <ul class="divide-y divide-border rounded-md border border-border">
      {#each backups as backup (backup.name)}
        <li class="flex items-center justify-between gap-3 px-3 py-2 text-sm">
          <span class="truncate">
            {new Date(backup.created_at).toLocaleString()}
            <span class="text-muted-foreground">
              · {formatFileSize(backup.size_bytes)}
            </span>
          </span>
          <Button
            variant="ghost"
            size="sm"
            class="cursor-pointer gap-2"
            href={`/api/settings/backups/${encodeURIComponent(backup.name)}`}
            download
          >
            <Download class="size-4" />
            Download
          </Button>
        </li>
      {/each}
    </ul>
  {/if}
</div>
