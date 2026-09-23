<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import ErrorBox from "$lib/components/error-box.svelte";
  import Notice from "$lib/components/notice.svelte";
  import CompactPagination from "$lib/components/compact-pagination.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import {
    BackgroundJobStatus,
    MediaType,
    type BackgroundJobRecord,
    type PaginatedLeftoversResponse,
    type UpgradeLeftover,
  } from "$lib/types/shared";
  import { formatFileSize } from "$lib/utils/formatters";
  import {
    createFilterState,
    createPerPageState,
    PER_PAGE_OPTIONS,
  } from "$lib/utils/pagination";
  import { toast } from "svelte-sonner";
  import { uiIndicators } from "$lib/stores/ui-indicators";
  import Search from "@lucide/svelte/icons/search";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import EyeOff from "@lucide/svelte/icons/eye-off";
  import Eye from "@lucide/svelte/icons/eye";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import Link2 from "@lucide/svelte/icons/link-2";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";

  interface Props {
    canManage: boolean;
  }
  let { canManage }: Props = $props();

  type SortBy = "title" | "size" | "imported";

  interface JobResult {
    succeeded?: number;
    failed?: number;
    freed_bytes?: number;
    errors?: string[];
  }

  const SORT_LABELS: Record<SortBy, string> = {
    title: "Title",
    size: "Largest first",
    imported: "Oldest import first",
  };

  const isSortBy = (v: unknown): v is SortBy =>
    v === "title" || v === "size" || v === "imported";
  const isBool = (v: unknown): v is boolean => typeof v === "boolean";

  const _sortStore = createFilterState<SortBy>(
    "leftovers_sort_by",
    "title",
    isSortBy,
  );
  const _manualStore = createFilterState<boolean>(
    "leftovers_show_manual",
    true,
    isBool,
  );
  const _ignoredStore = createFilterState<boolean>(
    "leftovers_show_ignored",
    false,
    isBool,
  );
  const _perPageStore = createPerPageState("leftovers_per_page");

  let data = $state<PaginatedLeftoversResponse | null>(null);
  let loading = $state(true);
  let error = $state("");
  let searchQuery = $state("");
  let sortBy = $state<SortBy>(_sortStore.getInitial());
  let includeManual = $state(_manualStore.getInitial());
  let includeIgnored = $state(_ignoredStore.getInitial());
  let perPage = $state(_perPageStore.getInitial());
  let currentPage = $state(1);
  let mounted = $state(false);

  let selectedIds = $state<Set<number>>(new Set());
  let confirmOpen = $state(false);
  let confirmItems = $state<UpgradeLeftover[]>([]);
  let submitting = $state(false);
  let scanRequested = $state(false);

  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  let scanTimer: ReturnType<typeof setTimeout> | null = null;
  let abortController: AbortController | null = null;

  const items = $derived(data?.items ?? []);
  const scanning = $derived(scanRequested || (data?.scan.running ?? false));

  $effect(() => _sortStore.save(sortBy));
  $effect(() => _manualStore.save(includeManual));
  $effect(() => _ignoredStore.save(includeIgnored));

  $effect(() => {
    sortBy;
    includeManual;
    includeIgnored;
    perPage;
    if (mounted) void load(1);
  });

  onMount(() => {
    mounted = true;
    return () => {
      if (scanTimer) clearTimeout(scanTimer);
    };
  });

  const load = async (page: number = currentPage) => {
    abortController?.abort();
    abortController = new AbortController();
    const signal = abortController.signal;
    loading = true;
    error = "";
    currentPage = page;
    selectedIds = new Set();
    try {
      const params = new URLSearchParams({
        page: page.toString(),
        per_page: perPage.toString(),
        sort_by: sortBy,
        include_manual: String(includeManual),
        include_ignored: String(includeIgnored),
      });
      if (searchQuery.trim()) params.append("search", searchQuery.trim());
      const response = await get_api<PaginatedLeftoversResponse>(
        `/api/duplicates/leftovers?${params.toString()}`,
        signal,
      );
      if (response.total_pages > 0 && page > response.total_pages) {
        await load(response.total_pages);
        return;
      }
      data = response;
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      error = e.message ?? "Failed to load upgrade leftovers.";
    } finally {
      if (!signal.aborted) loading = false;
    }
  };

  const handleSearch = (event: Event) => {
    searchQuery = (event.target as HTMLInputElement).value;
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => load(1), 400);
  };

  // backend datetimes are naive UTC
  const parseUtc = (value: string) =>
    new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);

  const formatDate = (value: string | null) =>
    value
      ? parseUtc(value).toLocaleDateString(undefined, {
          year: "numeric",
          month: "short",
          day: "numeric",
        })
      : "Unknown date";

  const label = (item: UpgradeLeftover) =>
    item.year ? `${item.title} (${item.year})` : item.title;

  const fileName = (path: string) => path.split(/[\\/]/).pop() ?? path;

  const folderName = (path: string) => {
    const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
    return cut > 0 ? path.slice(0, cut) : "";
  };

  const canDelete = (item: UpgradeLeftover) =>
    canManage && !item.manual_reason && !item.ignored;

  const freedBy = (list: UpgradeLeftover[]) =>
    list.reduce((sum, i) => sum + (i.frees_space ? i.size : 0), 0);

  // ---- selection ----

  const selectableOnPage = $derived(items.filter(canDelete));
  const allPageSelected = $derived(
    selectableOnPage.length > 0 &&
      selectableOnPage.every((i) => selectedIds.has(i.id)),
  );
  const somePageSelected = $derived(
    !allPageSelected && selectableOnPage.some((i) => selectedIds.has(i.id)),
  );
  const selectedItems = $derived(
    items.filter((i) => selectedIds.has(i.id) && canDelete(i)),
  );

  const toggleSelect = (id: number) => {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedIds = next;
  };

  const toggleSelectAll = () => {
    selectedIds = allPageSelected
      ? new Set()
      : new Set(selectableOnPage.map((i) => i.id));
  };

  // ---- actions ----

  const sleep = (ms: number) =>
    new Promise((resolve) => window.setTimeout(resolve, ms));

  const watchJob = async (jobId: number) => {
    for (let attempt = 0; attempt < 400; attempt += 1) {
      const job = await get_api<BackgroundJobRecord>(
        `/api/tasks/candidate-file-op-jobs/${jobId}`,
      );
      if (
        job.status === BackgroundJobStatus.Completed ||
        job.status === BackgroundJobStatus.Failed ||
        job.status === BackgroundJobStatus.Canceled
      ) {
        return job;
      }
      await sleep(1500);
    }
    return null;
  };

  const reportJob = (job: BackgroundJobRecord | null) => {
    if (!job) return;
    if (job.status !== BackgroundJobStatus.Completed) {
      toast.error(job.error_message ?? "Leftover cleanup did not finish.");
      return;
    }
    const result = ((job.payload ?? {}) as Record<string, unknown>).result as
      | JobResult
      | undefined;
    if (!result) return;
    if ((result.succeeded ?? 0) > 0) {
      toast.success(
        `Deleted ${result.succeeded} leftover${result.succeeded === 1 ? "" : "s"}, freed ${formatFileSize(result.freed_bytes ?? 0)}.`,
      );
    }
    if ((result.failed ?? 0) > 0) {
      const reasons = (result.errors ?? []).slice(0, 3).join("\n");
      toast.error(
        `${result.failed} leftover${result.failed === 1 ? "" : "s"} could not be deleted.${reasons ? `\n${reasons}` : ""}`,
      );
    }
  };

  const openConfirm = (targets: UpgradeLeftover[]) => {
    confirmItems = targets.filter(canDelete);
    if (confirmItems.length) confirmOpen = true;
  };

  const submitDelete = async () => {
    submitting = true;
    try {
      const response = await post_api<{ message: string; job_id: number }>(
        "/api/duplicates/leftovers/delete",
        { ids: confirmItems.map((i) => i.id) },
      );
      toast.success(response.message);
      confirmOpen = false;
      selectedIds = new Set();
      void watchJob(response.job_id).then(async (job) => {
        reportJob(job);
        await load(currentPage);
        uiIndicators.invalidate();
      });
    } catch (e: any) {
      toast.error(e.message ?? "Failed to queue leftover cleanup.");
    } finally {
      submitting = false;
    }
  };

  const setIgnored = async (item: UpgradeLeftover, ignored: boolean) => {
    try {
      await post_api(
        ignored
          ? "/api/duplicates/leftovers/ignore"
          : "/api/duplicates/leftovers/unignore",
        { id: item.id },
      );
      toast.success(ignored ? "Leftover ignored" : "Leftover restored");
      await load(currentPage);
    } catch (e: any) {
      toast.error(e.message ?? "Failed to update leftover.");
    }
  };

  // reload until the scan task finishes
  const pollScan = async () => {
    await load(currentPage);
    if (data?.scan.running) {
      scanTimer = setTimeout(pollScan, 3000);
    } else {
      scanRequested = false;
    }
  };

  const startScan = async () => {
    try {
      const response = await post_api<{ message: string }>(
        "/api/duplicates/leftovers/scan",
        {},
      );
      toast.success(response.message);
      scanRequested = true;
      scanTimer = setTimeout(pollScan, 1500);
    } catch (e: any) {
      toast.error(e.message ?? "Failed to start the scan.");
    }
  };
</script>

<AlertDialog.Root bind:open={confirmOpen}>
  <AlertDialog.Content class="text-foreground">
    <AlertDialog.Header>
      <AlertDialog.Title>
        Delete {confirmItems.length} leftover{confirmItems.length === 1
          ? ""
          : "s"}?
      </AlertDialog.Title>
      <AlertDialog.Description>
        {confirmItems.length === 1
          ? fileName(confirmItems[0].dropped_path)
          : `${confirmItems.length} files`} will be deleted from Radarr's download
        folder, freeing {formatFileSize(freedBy(confirmItems))}. The movie's
        current file is not touched. A release folder left holding only extras
        (.nfo, samples, subtitles) is removed too. This cannot be undone.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel disabled={submitting}>Cancel</AlertDialog.Cancel>
      <AlertDialog.Action
        class="bg-destructive text-white hover:bg-destructive/90"
        disabled={submitting}
        onclick={submitDelete}
      >
        {submitting ? "Queuing..." : "Delete"}
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<div class="space-y-4">
  <div class="flex flex-wrap items-center justify-between gap-3">
    <p class="text-sm text-muted-foreground">
      Files Radarr replaced with an upgrade that are still in its download
      folder.
      {#if data?.scan.scanned_at}
        Last scan: {parseUtc(data.scan.scanned_at).toLocaleString()}.
      {:else if data}
        Not scanned yet.
      {/if}
    </p>
    {#if canManage}
      <Button
        variant="outline"
        class="cursor-pointer"
        disabled={scanning}
        onclick={startScan}
      >
        <RefreshCw class="size-4 {scanning ? 'animate-spin' : ''}" />
        {scanning ? "Scanning..." : "Scan now"}
      </Button>
    {/if}
  </div>

  {#if data && data.scan.unmapped_roots.length > 0}
    <Notice type="warning" title="Some download folders couldn't be checked">
      Reclaimerr can't reach these Radarr download folders, so leftovers in them
      are not listed. Add a path mapping for each in Settings:
      <ul class="mt-1.5 list-disc pl-5 font-mono text-xs">
        {#each data.scan.unmapped_roots as root}
          <li class="break-all">{root}</li>
        {/each}
      </ul>
    </Notice>
  {/if}

  {#if data && data.summary.leftovers > 0}
    <div class="grid grid-cols-3 gap-2">
      <div class="rounded-lg border border-border bg-card px-4 py-3">
        <p class="text-xs text-muted-foreground">Leftovers</p>
        <p class="text-xl font-semibold text-foreground">
          {data.summary.leftovers}
        </p>
      </div>
      <div class="rounded-lg border border-border bg-card px-4 py-3">
        <p class="text-xs text-muted-foreground">Ready to clean up</p>
        <p class="text-xl font-semibold text-foreground">
          {data.summary.actionable}
        </p>
      </div>
      <div class="rounded-lg border border-border bg-card px-4 py-3">
        <p class="text-xs text-muted-foreground">Reclaimable</p>
        <p class="text-xl font-semibold text-foreground">
          {formatFileSize(data.summary.reclaimable_size)}
        </p>
      </div>
    </div>
  {/if}

  <!-- filters -->
  <div class="flex flex-col gap-2 lg:flex-row">
    <div class="relative flex-1">
      <Search
        class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground"
      />
      <Input
        type="text"
        placeholder="Search by title"
        value={searchQuery}
        oninput={handleSearch}
        class="pl-10 bg-card"
      />
    </div>
    <div class="flex flex-1 gap-2">
      <Select.Root
        type="single"
        value={sortBy}
        onValueChange={(v) => {
          if (isSortBy(v)) sortBy = v;
        }}
      >
        <Select.Trigger class="flex-1 bg-card text-card-foreground">
          {SORT_LABELS[sortBy]}
        </Select.Trigger>
        <Select.Content class="bg-card">
          {#each Object.entries(SORT_LABELS) as [value, text]}
            <Select.Item {value} label={text} class="text-card-foreground">
              {text}
            </Select.Item>
          {/each}
        </Select.Content>
      </Select.Root>
      <Select.Root
        type="single"
        value={perPage.toString()}
        onValueChange={(v) => {
          const n = parseInt(v, 10);
          if (!isNaN(n)) {
            perPage = n;
            _perPageStore.save(n);
          }
        }}
      >
        <Select.Trigger class="flex-1 bg-card text-card-foreground">
          {perPage} / page
        </Select.Trigger>
        <Select.Content class="bg-card">
          {#each PER_PAGE_OPTIONS as opt}
            <Select.Item
              value={opt.toString()}
              label={`${opt} / page`}
              class="text-card-foreground"
            >
              {opt} / page
            </Select.Item>
          {/each}
        </Select.Content>
      </Select.Root>
    </div>
  </div>

  <div class="flex flex-wrap gap-x-6 gap-y-2 text-sm text-foreground">
    <label class="flex items-center gap-2 cursor-pointer">
      <Switch bind:checked={includeManual} />
      Show items that need manual review
    </label>
    <label class="flex items-center gap-2 cursor-pointer">
      <Switch bind:checked={includeIgnored} />
      Show ignored
    </label>
  </div>

  {#if canManage && !loading && selectableOnPage.length > 0}
    <div
      class="flex flex-col gap-2 rounded-lg border border-border bg-muted/35 px-4 py-2.5
        sm:flex-row sm:items-center sm:justify-between"
    >
      <label
        class="flex items-center gap-2 text-sm font-medium text-foreground cursor-pointer"
      >
        <input
          type="checkbox"
          checked={allPageSelected}
          indeterminate={somePageSelected}
          onchange={toggleSelectAll}
          class="cursor-pointer accent-primary"
        />
        Select all on this page
      </label>
      <span class="text-xs text-muted-foreground sm:text-sm">
        {selectedItems.length} of {selectableOnPage.length} selected
      </span>
    </div>
  {/if}

  {#if canManage && selectedItems.length > 0}
    <div
      class="flex items-center justify-between gap-4 px-4 py-3 bg-primary/10 border border-primary/30 rounded-lg"
    >
      <span class="text-sm text-foreground font-medium">
        {selectedItems.length} leftover{selectedItems.length === 1 ? "" : "s"} selected
        <span class="text-muted-foreground font-normal">
          - {formatFileSize(freedBy(selectedItems))} reclaimable
        </span>
      </span>
      <Button
        size="sm"
        variant="destructive"
        class="cursor-pointer"
        onclick={() => openConfirm(selectedItems)}
      >
        <Trash2 class="size-4" />
        Delete selected
      </Button>
    </div>
  {/if}

  <ErrorBox {error} />

  <div class="bg-card rounded-lg border border-border">
    {#if loading && !data}
      <div class="p-8 text-center text-muted-foreground">
        <div
          class="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-primary border-r-transparent"
        ></div>
        <p class="mt-4">Loading upgrade leftovers...</p>
      </div>
    {:else if items.length === 0}
      <div class="p-8 text-center text-muted-foreground">
        No upgrade leftovers found.
      </div>
    {:else}
      <ul class="divide-y divide-border">
        {#each items as item (item.id)}
          <li class="flex items-start gap-3 p-3 md:p-4">
            {#if canManage}
              <input
                type="checkbox"
                class="mt-1 cursor-pointer accent-primary disabled:cursor-not-allowed"
                checked={selectedIds.has(item.id)}
                disabled={!canDelete(item)}
                onchange={() => toggleSelect(item.id)}
                aria-label="Select {label(item)}"
              />
            {/if}
            <PosterThumb
              mediaType={MediaType.Movie}
              posterUrl={item.poster_url}
              tailWindElSize="w-12"
            />
            <div class="flex-1 min-w-0">
              <p class="font-semibold text-foreground truncate">
                {label(item)}
              </p>
              <p class="mt-0.5 text-xs text-foreground break-all">
                {fileName(item.dropped_path)}
              </p>
              {#if folderName(item.dropped_path)}
                <p class="text-xs font-mono text-muted-foreground break-all">
                  {folderName(item.dropped_path)}
                </p>
              {/if}
              <div
                class="flex flex-wrap items-center gap-1.5 mt-1 text-xs text-muted-foreground"
              >
                <span class="font-medium text-foreground">
                  {formatFileSize(item.size)}
                </span>
                <span>· imported {formatDate(item.imported_at)}</span>
                {#if !item.frees_space}
                  <span
                    class="flex items-center gap-1 px-2 rounded-full border border-border"
                    title="Another hardlink keeps this data on disk, so deleting it frees no space"
                  >
                    <Link2 class="size-3" /> Hardlinked, frees no space
                  </span>
                {/if}
                {#if item.ignored}
                  <span class="px-2 rounded-full border border-border">
                    Ignored
                  </span>
                {/if}
              </div>
              {#if item.manual_reason}
                <p
                  class="mt-1.5 flex items-center gap-1.5 text-sm text-amber-600 dark:text-amber-400"
                >
                  <TriangleAlert class="size-4 shrink-0" />
                  Manual review: {item.manual_reason}
                </p>
              {/if}
            </div>
            {#if canManage}
              <div class="flex flex-wrap justify-end gap-2">
                {#if item.ignored}
                  <Button
                    size="sm"
                    variant="outline"
                    class="cursor-pointer"
                    onclick={() => setIgnored(item, false)}
                  >
                    <Eye class="size-4" />
                    Restore
                  </Button>
                {:else}
                  <Button
                    size="sm"
                    variant="outline"
                    class="cursor-pointer"
                    onclick={() => setIgnored(item, true)}
                  >
                    <EyeOff class="size-4" />
                    Ignore
                  </Button>
                {/if}
                <Button
                  size="sm"
                  variant="destructive"
                  class="cursor-pointer"
                  disabled={!canDelete(item)}
                  onclick={() => openConfirm([item])}
                >
                  <Trash2 class="size-4" />
                  Delete
                </Button>
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  {#if data && data.total_pages > 1}
    <div
      class="flex flex-wrap justify-center gap-2 md:flex-nowrap md:justify-between items-center"
    >
      <p class="text-sm text-muted-foreground">
        Showing {(data.page - 1) * data.per_page + 1} to {Math.min(
          data.page * data.per_page,
          data.total,
        )} of {data.total} results
      </p>
      <CompactPagination
        currentPage={data.page}
        totalPages={data.total_pages}
        maxVisiblePages={3}
        onPageChange={load}
      />
    </div>
  {/if}
</div>
