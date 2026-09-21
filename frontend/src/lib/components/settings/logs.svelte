<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import type { Component } from "svelte";
  import { get_api } from "$lib/api";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import Download from "@lucide/svelte/icons/download";
  import RotateCw from "@lucide/svelte/icons/rotate-cw";
  import { formatFileSize } from "$lib/utils/formatters";
  import type { LogFileInfo, LogTailResponse } from "$lib/types/shared";

  interface Props {
    svgIcon: Component | null;
  }

  let { svgIcon }: Props = $props();

  const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;
  const LINE_OPTIONS = ["100", "250", "500", "1000", "2500", "5000"] as const;
  const REFRESH_MS = 10_000;

  let files = $state<LogFileInfo[]>([]);
  let tail = $state<LogTailResponse | null>(null);
  let loading = $state(true);
  let refreshing = $state(false);
  let loadError = $state("");

  let selectedFile = $state("");
  let level = $state("ALL");
  let search = $state("");
  let lines = $state("500");
  let autoRefresh = $state(false);

  let timer: ReturnType<typeof setInterval> | null = null;
  let searchDebounce: ReturnType<typeof setTimeout> | null = null;

  const levelClass = (value: string | null) => {
    switch (value) {
      case "CRITICAL":
      case "ERROR":
        return "text-destructive";
      case "WARNING":
        return "text-yellow-500";
      case "DEBUG":
        return "text-muted-foreground";
      default:
        return "text-primary";
    }
  };

  async function loadFiles() {
    try {
      files = await get_api<LogFileInfo[]>("/api/settings/logs/files");
      if (!selectedFile && files.length > 0) selectedFile = files[0].name;
    } catch (e) {
      loadError = e instanceof Error ? e.message : "Failed to list log files";
    }
  }

  async function loadTail() {
    refreshing = true;
    try {
      const params = new URLSearchParams({ lines });
      if (selectedFile) params.set("file", selectedFile);
      if (level !== "ALL") params.set("level", level);
      if (search.trim()) params.set("search", search.trim());
      tail = await get_api<LogTailResponse>(`/api/settings/logs?${params}`);
      loadError = "";
    } catch (e) {
      loadError = e instanceof Error ? e.message : "Failed to load the log";
      tail = null;
    } finally {
      refreshing = false;
    }
  }

  function downloadUrl(): string {
    const params = new URLSearchParams();
    if (selectedFile) params.set("file", selectedFile);
    return `/api/settings/logs/download?${params}`;
  }

  function onSearchInput() {
    if (searchDebounce) clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => void loadTail(), 400);
  }

  $effect(() => {
    // re-arm whenever the toggle flips
    autoRefresh;
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    if (autoRefresh) timer = setInterval(() => void loadTail(), REFRESH_MS);
  });

  onMount(async () => {
    await loadFiles();
    await loadTail();
    loading = false;
  });

  onDestroy(() => {
    if (timer) clearInterval(timer);
    if (searchDebounce) clearTimeout(searchDebounce);
  });
</script>

<div class="space-y-6">
  <div>
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {#if svgIcon}
        {@const Icon = svgIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      Logs
    </h2>
    <p class="text-sm text-muted-foreground mt-1">
      Read the application log without leaving Reclaimerr. Set the verbosity
      with the <code class="text-xs">LOG_LEVEL</code> environment variable.
    </p>
  </div>

  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner class="w-12 h-12 text-primary" />
    </div>
  {:else}
    {#if loadError}
      <div
        class="rounded-md border border-destructive/40 bg-destructive/10 p-3"
      >
        <p class="text-sm text-destructive">{loadError}</p>
      </div>
    {/if}

    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div class="flex flex-col gap-1.5">
        <Label for="log-file">File</Label>
        <Select.Root
          type="single"
          bind:value={selectedFile}
          onValueChange={() => void loadTail()}
        >
          <Select.Trigger
            id="log-file"
            class="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
          >
            {selectedFile || "Current"}
          </Select.Trigger>
          <Select.Content class="bg-card border-ring">
            {#each files as item (item.name)}
              <Select.Item
                value={item.name}
                label={item.name}
                class="text-foreground"
              >
                {item.name}{item.is_current ? " (current)" : ""}
              </Select.Item>
            {/each}
          </Select.Content>
        </Select.Root>
      </div>

      <div class="flex flex-col gap-1.5">
        <Label for="log-level">Minimum level</Label>
        <Select.Root
          type="single"
          bind:value={level}
          onValueChange={() => void loadTail()}
        >
          <Select.Trigger
            id="log-level"
            class="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
          >
            {level === "ALL" ? "All levels" : level}
          </Select.Trigger>
          <Select.Content class="bg-card border-ring">
            <Select.Item value="ALL" label="All levels" class="text-foreground">
              All levels
            </Select.Item>
            {#each LEVELS as item (item)}
              <Select.Item value={item} label={item} class="text-foreground">
                {item} and above
              </Select.Item>
            {/each}
          </Select.Content>
        </Select.Root>
      </div>

      <div class="flex flex-col gap-1.5">
        <Label for="log-lines">Lines</Label>
        <Select.Root
          type="single"
          bind:value={lines}
          onValueChange={() => void loadTail()}
        >
          <Select.Trigger
            id="log-lines"
            class="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
          >
            {lines}
          </Select.Trigger>
          <Select.Content class="bg-card border-ring">
            {#each LINE_OPTIONS as item (item)}
              <Select.Item value={item} label={item} class="text-foreground">
                {item}
              </Select.Item>
            {/each}
          </Select.Content>
        </Select.Root>
      </div>

      <div class="flex flex-col gap-1.5">
        <Label for="log-search">Search</Label>
        <Input
          id="log-search"
          bind:value={search}
          oninput={onSearchInput}
          placeholder="Match any text in the entry"
        />
      </div>
    </div>

    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-4">
        <div class="flex items-center gap-2">
          <Switch id="log-auto-refresh" bind:checked={autoRefresh} />
          <Label for="log-auto-refresh" class="text-sm">
            Auto refresh every 10s
          </Label>
        </div>
        {#if tail}
          <span class="text-xs text-muted-foreground">
            {tail.entries.length} shown · file is {formatFileSize(
              tail.file_size_bytes,
            )} · logging at {tail.configured_level}
          </span>
        {/if}
      </div>
      <div class="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onclick={() => void loadTail()}
          disabled={refreshing}
        >
          <RotateCw class="h-4 w-4 {refreshing ? 'animate-spin' : ''}" />
          Refresh
        </Button>
        <Button variant="outline" size="sm" href={downloadUrl()} download>
          <Download class="h-4 w-4" />
          Download
        </Button>
      </div>
    </div>

    {#if tail?.scan_truncated}
      <p class="text-xs text-muted-foreground">
        This file is larger than one request reads, so only its most recent
        portion was searched. Download it to see everything.
      </p>
    {/if}

    <div
      class="rounded-md border border-border bg-background/60 max-h-[32rem] overflow-auto"
    >
      {#if !tail || tail.entries.length === 0}
        <p class="p-4 text-sm text-muted-foreground">
          Nothing matched. Try a lower level or a different search.
        </p>
      {:else}
        <ul class="divide-y divide-border/60 font-mono text-xs">
          {#each tail.entries as entry, index (index)}
            <li class="flex gap-2 px-3 py-1.5">
              <span class="shrink-0 text-muted-foreground">
                {entry.timestamp ?? ""}
              </span>
              <span class="w-16 shrink-0 {levelClass(entry.level)}">
                {entry.level ?? ""}
              </span>
              <span
                class="min-w-0 whitespace-pre-wrap break-words text-foreground"
              >
                {#if entry.task_child}
                  <Badge variant="secondary" class="mr-1 px-1 text-[10px]">
                    task
                  </Badge>
                {/if}{entry.message}
              </span>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</div>
