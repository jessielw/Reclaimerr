<script lang="ts">
  import { onMount } from "svelte";
  import Gauge from "@lucide/svelte/icons/gauge";
  import RotateCw from "@lucide/svelte/icons/rotate-cw";
  import { get_api, put_api } from "$lib/api";
  import { auth } from "$lib/stores/auth";
  import ErrorBox from "$lib/components/error-box.svelte";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { formatFileSize } from "$lib/utils/formatters";
  import { toTitleCase } from "$lib/utils/strings";
  import type { StorageMount, StorageResponse } from "$lib/types/shared";
  import { formatDateTimeToLocaleString } from "$lib/utils/date";

  let data = $state<StorageResponse | null>(null);
  let loading = $state(true);
  let error = $state("");
  let lastUpdated = $state<string | null>(null);
  let saving = $state(false);

  const usedPercent = (used: number, total: number) =>
    total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;

  /** Red once a volume is nearly full, so a problem stands out at a glance. */
  const barClass = (percent: number) => {
    if (percent >= 90) return "bg-destructive";
    if (percent >= 75) return "bg-yellow-500";
    return "bg-primary";
  };

  const actionLabel = (action: string) =>
    toTitleCase(action.replace(/_/g, " "));

  async function load() {
    if (loading && data) return;
    loading = true;
    error = "";
    try {
      data = await get_api<StorageResponse>("/api/storage");
      lastUpdated = new Date().toISOString();
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to load storage metrics";
    } finally {
      loading = false;
    }
  }

  async function saveMount(mount: StorageMount, mode: string) {
    if (saving || loading) return;
    saving = true;
    error = "";
    try {
      await put_api("/api/storage/mounts", {
        mode,
        targets: mount.sources.map((source) => ({
          service_type: source.service_type,
          service_config_id: source.service_config_id,
          path: mount.path,
        })),
      });
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to save capacity choice";
    } finally {
      saving = false;
    }
  }

  onMount(load);
</script>

<div class="p-2.5 md:p-8">
  <div class="max-w-7xl mx-auto space-y-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1
          class="text-3xl font-bold text-foreground flex items-center gap-2 flex-wrap"
        >
          <Gauge class="h-7 w-7 text-primary" />
          Storage
        </h1>
        <p class="text-muted-foreground">
          Disk capacity as Radarr and Sonarr see it, library size, and what
          cleanup has reclaimed
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-3">
        {#if lastUpdated}
          <p class="text-xs text-muted-foreground" role="status">
            Last updated: {formatDateTimeToLocaleString(lastUpdated, {
              includeTimezone: true,
            })}
            {#if error}
              · Showing previous metrics{/if}
          </p>
        {/if}
        <Button
          variant="outline"
          size="sm"
          onclick={load}
          disabled={loading || saving}
        >
          <RotateCw class="h-4 w-4 {loading ? 'animate-spin' : ''}" />
          Refresh
        </Button>
      </div>
    </div>

    <ErrorBox {error} />

    {#if loading && !data}
      <div
        class="bg-card rounded-lg border border-border p-8 text-center text-muted-foreground"
      >
        Loading storage metrics...
      </div>
    {:else if data}
      {#if data.errors.length > 0}
        <div
          class="rounded-md border border-yellow-600/40 bg-yellow-500/10 p-3 space-y-1"
        >
          {#each data.errors as message (message)}
            <p class="text-sm text-yellow-600 dark:text-yellow-400">
              {message}
            </p>
          {/each}
        </div>
      {/if}

      <!-- headline totals -->
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div class="bg-card rounded-lg border border-border p-4">
          <p class="text-sm text-muted-foreground">Capacity</p>
          <p class="text-2xl font-bold text-foreground mt-1">
            {formatFileSize(data.capacity_total_bytes)}
          </p>
          {#if data.capacity_incomplete}
            <p class="text-xs text-muted-foreground mt-1">
              Some mounts report no total, so this is a floor
            </p>
          {/if}
        </div>
        <div class="bg-card rounded-lg border border-border p-4">
          <p class="text-sm text-muted-foreground">Free</p>
          <p class="text-2xl font-bold text-green-500 mt-1">
            {formatFileSize(data.capacity_free_bytes)}
          </p>
        </div>
        <div class="bg-card rounded-lg border border-border p-4">
          <p class="text-sm text-muted-foreground">Reclaimable now</p>
          <p class="text-2xl font-bold text-primary mt-1">
            {formatFileSize(data.reclaimable_total_bytes)}
          </p>
        </div>
        <div class="bg-card rounded-lg border border-border p-4">
          <p class="text-sm text-muted-foreground">Reclaimed to date</p>
          <p class="text-2xl font-bold text-green-500 mt-1">
            {formatFileSize(data.reclaimed_total_bytes)}
          </p>
        </div>
      </div>

      <!-- mounts -->
      <div class="bg-card rounded-lg border border-border">
        <div class="border-b border-border p-3">
          <h2 class="font-semibold text-foreground">Mounts</h2>
          <p class="text-xs text-muted-foreground">
            Matching total and free space are counted once automatically. For
            pooled storage, exclude the branches or the pool to avoid counting
            both. Include can also count separate disks with matching sizes.
          </p>
        </div>
        {#if data.mounts.length === 0}
          <p class="p-4 text-sm text-muted-foreground">
            No mounts were reported. Configure Radarr or Sonarr under Settings
            to see capacity here.
          </p>
        {:else}
          <ul class="divide-y divide-border">
            {#each data.mounts as mount (mount.path + (mount.total_bytes ?? ""))}
              {@const percent = usedPercent(
                mount.used_bytes ?? 0,
                mount.total_bytes ?? 0,
              )}
              <li class="p-3 space-y-2">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <div class="min-w-0">
                    <p class="truncate font-mono text-sm text-foreground">
                      {mount.path}
                    </p>
                    <div class="mt-1 flex flex-wrap items-center gap-1">
                      {#if mount.label}
                        <Badge variant="outline" class="text-[10px]">
                          {mount.label}
                        </Badge>
                      {/if}
                      {#each mount.sources as source (source.service_type + source.service_config_id)}
                        <Badge variant="secondary" class="text-[10px]">
                          {source.name}
                        </Badge>
                      {/each}
                      <Badge
                        variant={mount.included_in_capacity
                          ? "outline"
                          : "secondary"}
                        class="text-[10px]"
                      >
                        {mount.included_in_capacity
                          ? "Counted in capacity"
                          : "Excluded from capacity"}
                      </Badge>
                    </div>
                  </div>
                  <p class="text-sm text-muted-foreground">
                    {#if mount.total_bytes != null}
                      {formatFileSize(mount.used_bytes)} of {formatFileSize(
                        mount.total_bytes,
                      )} used · {formatFileSize(mount.free_bytes)} free
                    {:else}
                      {formatFileSize(mount.free_bytes)} free · total unknown
                    {/if}
                  </p>
                </div>
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <p class="text-xs text-muted-foreground">
                    {#if mount.duplicate_of}
                      Same reported capacity and free space as {mount.duplicate_of}
                    {:else if mount.capacity_mode === "exclude"}
                      Excluded by an administrator
                    {/if}
                  </p>
                  {#if $auth.user?.role === "admin"}
                    <label
                      class="flex items-center gap-2 text-xs text-muted-foreground"
                    >
                      Capacity
                      <select
                        class="rounded-md border border-input bg-background px-2 py-1 text-foreground"
                        aria-label={`Capacity for ${mount.path} (${mount.sources.map((source) => source.name).join(", ")})`}
                        value={mount.capacity_mode}
                        disabled={saving || loading}
                        onchange={async (event) => {
                          const select = event.currentTarget;
                          const mode = select.value;
                          select.value = mount.capacity_mode;
                          await saveMount(mount, mode);
                        }}
                      >
                        <option value="auto">Automatic</option>
                        <option value="include">Include</option>
                        <option value="exclude">Exclude</option>
                      </select>
                    </label>
                  {/if}
                </div>
                {#if mount.total_bytes != null}
                  <div class="h-2 w-full rounded-full bg-secondary">
                    <div
                      class="h-2 rounded-full {barClass(percent)}"
                      style="width: {percent}%"
                    ></div>
                  </div>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </div>

      <div class="grid gap-4 lg:grid-cols-2">
        <!-- per instance -->
        <div class="bg-card rounded-lg border border-border">
          <div class="border-b border-border p-3">
            <h2 class="font-semibold text-foreground">By instance</h2>
            <p class="text-xs text-muted-foreground">
              Capacity after mount choices and duplicate detection
            </p>
          </div>
          {#if data.instances.length === 0}
            <p class="p-4 text-sm text-muted-foreground">
              Nothing to show yet.
            </p>
          {:else}
            <ul class="divide-y divide-border">
              {#each data.instances as instance (instance.service_type + instance.service_config_id)}
                <li
                  class="flex flex-wrap items-center justify-between gap-2 p-3"
                >
                  <div>
                    <p class="text-sm text-foreground">{instance.name}</p>
                    <p class="text-xs text-muted-foreground">
                      {toTitleCase(instance.service_type)} · {instance.mount_count}
                      {instance.mount_count === 1 ? "mount" : "mounts"}
                    </p>
                  </div>
                  <p class="text-sm text-muted-foreground">
                    {formatFileSize(instance.used_bytes)} of {formatFileSize(
                      instance.total_bytes,
                    )} used
                  </p>
                </li>
              {/each}
            </ul>
          {/if}
        </div>

        <!-- library -->
        <div class="bg-card rounded-lg border border-border">
          <div class="border-b border-border p-3">
            <h2 class="font-semibold text-foreground">Library</h2>
          </div>
          <ul class="divide-y divide-border">
            {#each data.libraries as entry (entry.media_type)}
              <li class="flex items-center justify-between gap-2 p-3">
                <div>
                  <p class="text-sm text-foreground">
                    {toTitleCase(entry.media_type)}
                  </p>
                  <p class="text-xs text-muted-foreground">
                    {entry.item_count.toLocaleString()} items
                  </p>
                </div>
                <p class="text-sm text-muted-foreground">
                  {formatFileSize(entry.total_bytes)}
                </p>
              </li>
            {/each}
            <li class="flex items-center justify-between gap-2 p-3">
              <p class="text-sm font-medium text-foreground">Total</p>
              <p class="text-sm font-medium text-foreground">
                {formatFileSize(data.library_total_bytes)}
              </p>
            </li>
          </ul>
        </div>
      </div>

      <div class="grid gap-4 lg:grid-cols-2">
        <!-- reclaimable -->
        <div class="bg-card rounded-lg border border-border">
          <div class="border-b border-border p-3">
            <h2 class="font-semibold text-foreground">Reclaimable</h2>
            <p class="text-xs text-muted-foreground">
              Space currently held by cleanup candidates
            </p>
          </div>
          {#if data.reclaimable.length === 0}
            <p class="p-4 text-sm text-muted-foreground">
              No cleanup candidates right now.
            </p>
          {:else}
            <ul class="divide-y divide-border">
              {#each data.reclaimable as entry (entry.media_type)}
                <li class="flex items-center justify-between gap-2 p-3">
                  <div>
                    <p class="text-sm text-foreground">
                      {toTitleCase(entry.media_type)}
                    </p>
                    <p class="text-xs text-muted-foreground">
                      {entry.candidate_count.toLocaleString()} candidates
                    </p>
                  </div>
                  <p class="text-sm text-primary">
                    {formatFileSize(entry.total_bytes)}
                  </p>
                </li>
              {/each}
            </ul>
          {/if}
        </div>

        <!-- reclaimed -->
        <div class="bg-card rounded-lg border border-border">
          <div class="border-b border-border p-3">
            <h2 class="font-semibold text-foreground">Reclaimed</h2>
            <p class="text-xs text-muted-foreground">
              Everything Reclaimerr has acted on, by outcome
            </p>
          </div>
          {#if data.reclaimed.length === 0}
            <p class="p-4 text-sm text-muted-foreground">
              Nothing has been reclaimed yet.
            </p>
          {:else}
            <ul class="divide-y divide-border">
              {#each data.reclaimed as entry (entry.media_type + entry.action)}
                <li class="flex items-center justify-between gap-2 p-3">
                  <div>
                    <p class="text-sm text-foreground">
                      {toTitleCase(entry.media_type)} · {actionLabel(
                        entry.action,
                      )}
                    </p>
                    <p class="text-xs text-muted-foreground">
                      {entry.item_count.toLocaleString()} items
                    </p>
                  </div>
                  <p class="text-sm text-green-500">
                    {formatFileSize(entry.total_bytes)}
                  </p>
                </li>
              {/each}
            </ul>
          {/if}
        </div>
      </div>
    {/if}
  </div>
</div>
