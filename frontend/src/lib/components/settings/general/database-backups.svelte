<script lang="ts">
  import { onMount } from "svelte";
  import { delete_api, get_api, post_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import Upload from "@lucide/svelte/icons/upload";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
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
  let directory = $state<string | null>(null);
  let restorePending = $state(false);
  let error = $state<string | null>(null);

  // what the confirm dialog will restore: a stored backup or an uploaded file
  let restoreTarget = $state<{
    name: string;
    label: string;
    file: File | null;
  } | null>(null);
  let restoring = $state(false);
  let uploadInput = $state<HTMLInputElement | null>(null);

  async function load() {
    try {
      const data = await get_api<{
        directory: string;
        backups: DatabaseBackupInfo[];
        restore_pending: boolean;
      }>("/api/settings/backups");
      backups = data.backups;
      directory = data.directory;
      restorePending = data.restore_pending;
    } catch (e: any) {
      error = e.message ?? "Could not load backups.";
    }
  }

  onMount(load);

  function askRestore(backup: DatabaseBackupInfo) {
    restoreTarget = {
      name: backup.name,
      label: new Date(backup.created_at).toLocaleString(),
      file: null,
    };
  }

  function onUploadPicked(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (file) restoreTarget = { name: file.name, label: file.name, file };
  }

  async function confirmRestore() {
    const target = restoreTarget;
    if (!target) return;
    restoring = true;
    try {
      if (target.file) {
        const form = new FormData();
        form.append("file", target.file);
        await post_api("/api/settings/backups/restore", form);
      } else {
        await post_api(
          `/api/settings/backups/${encodeURIComponent(target.name)}/restore`,
        );
      }
      restorePending = true;
      toast.success("Restore staged. Restart Reclaimerr to apply it.");
    } catch (e: any) {
      toast.error(e.message ?? "Could not stage the restore.");
    } finally {
      restoring = false;
      restoreTarget = null;
    }
  }

  async function cancelRestore() {
    try {
      await delete_api("/api/settings/backups/restore");
      restorePending = false;
      toast.success("Restore cancelled.");
    } catch (e: any) {
      toast.error(e.message ?? "Could not cancel the restore.");
    }
  }
</script>

<div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
  <h3 class="font-semibold text-foreground mb-1">Database Backups</h3>
  <p class="text-muted-foreground text-sm mb-3">
    The <strong>Back Up Database</strong> task saves a copy of the database to
    {#if directory}<code>{directory}</code>{:else}the backups folder{/if} every week.
    Set <code>BACKUP_DIR</code> to change the location. You can also download a
    copy now. A restore is applied the next time Reclaimerr starts, and needs
    the original <code>secrets.env</code> if this is a new install.
  </p>

  {#if restorePending}
    <Notice type="warning" title="Restore staged" class="mb-3">
      <p>
        The database will be replaced with the chosen backup the next time
        Reclaimerr starts. The current database is kept as
        <code>reclaimerr.db.pre-restore</code>.
      </p>
      <Button variant="outline" size="sm" class="mt-2" onclick={cancelRestore}>
        Cancel restore
      </Button>
    </Notice>
  {/if}

  <Notice type="warning" title="Keep backups private" class="mb-3">
    Backups contain service API keys, media server tokens, and email settings.
  </Notice>

  <div class="flex flex-wrap items-end gap-4 mb-3">
    <Button
      variant="outline"
      size="sm"
      class="gap-2"
      href="/api/settings/backups/download"
      download
    >
      <Download class="size-4" />
      Download backup now
    </Button>
    <Button
      variant="outline"
      size="sm"
      class="gap-2"
      onclick={() => uploadInput?.click()}
    >
      <Upload class="size-4" />
      Restore from file
    </Button>
    <input
      bind:this={uploadInput}
      type="file"
      accept=".zip,.db,application/zip,application/vnd.sqlite3,application/octet-stream"
      class="hidden"
      onchange={onUploadPicked}
    />
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
          <span class="flex shrink-0 gap-1">
            <Button
              variant="ghost"
              size="sm"
              class="gap-2"
              onclick={() => askRestore(backup)}
            >
              <RotateCcw class="size-4" />
              Restore
            </Button>
            <Button
              variant="ghost"
              size="sm"
              class="gap-2"
              href={`/api/settings/backups/${encodeURIComponent(backup.name)}`}
              download
            >
              <Download class="size-4" />
              Download
            </Button>
          </span>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<AlertDialog.Root
  open={restoreTarget !== null}
  onOpenChange={(v) => {
    if (!v) restoreTarget = null;
  }}
>
  <AlertDialog.Content
    class="bg-card border border-border rounded-lg p-6 max-w-md w-full text-foreground"
  >
    <AlertDialog.Header>
      <AlertDialog.Title class="text-xl font-semibold text-foreground mb-2">
        Restore database backup
      </AlertDialog.Title>
      <AlertDialog.Description class="text-muted-foreground">
        Stage "{restoreTarget?.label}" to replace the current database the next
        time Reclaimerr starts. Everything changed since that backup is lost,
        though the current database is kept as
        <code>reclaimerr.db.pre-restore</code>. Nothing changes until you
        restart.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer class="flex justify-end gap-3 pt-4">
      <AlertDialog.Cancel class="hover text-foreground bg-secondary">
        Cancel
      </AlertDialog.Cancel>
      <AlertDialog.Action
        class="hover"
        disabled={restoring}
        onclick={confirmRestore}
      >
        Stage restore
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
