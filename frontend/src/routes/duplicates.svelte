<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api, put_api } from "$lib/api";
  import ErrorBox from "$lib/components/error-box.svelte";
  import CompactPagination from "$lib/components/compact-pagination.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import { auth } from "$lib/stores/auth";
  import {
    BackgroundJobStatus,
    MediaType,
    Permission,
    UserRole,
    type BackgroundJobRecord,
    type DuplicateFile,
    type DuplicateGroup,
    type DuplicateKeeperPriorityEntry,
    type PaginatedDuplicatesResponse,
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
  import Lock from "@lucide/svelte/icons/lock";
  import EyeOff from "@lucide/svelte/icons/eye-off";
  import Eye from "@lucide/svelte/icons/eye";
  import SlidersHorizontal from "@lucide/svelte/icons/sliders-horizontal";
  import ArrowUp from "@lucide/svelte/icons/arrow-up";
  import ArrowDown from "@lucide/svelte/icons/arrow-down";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";

  type MediaFilter = "all" | MediaType.Movie | MediaType.Series;
  type SortBy = "title" | "size";

  interface DuplicateJobResult {
    succeeded?: number;
    failed?: number;
    freed_bytes?: number;
    errors?: string[];
  }

  const CRITERION_LABELS: Record<string, string> = {
    resolution: "Higher resolution",
    dolby_vision: "Dolby Vision",
    hdr: "HDR",
    video_codec: "Newer video codec (AV1 > HEVC > H.264)",
    audio_channels: "More audio channels",
    video_bitrate: "Higher video bitrate",
    size_larger: "Larger file",
    size_smaller: "Smaller file",
  };

  const isMediaFilter = (v: unknown): v is MediaFilter =>
    v === "all" || v === MediaType.Movie || v === MediaType.Series;
  const isSortBy = (v: unknown): v is SortBy => v === "title" || v === "size";
  const isBool = (v: unknown): v is boolean => typeof v === "boolean";

  const _mediaStore = createFilterState<MediaFilter>(
    "duplicates_media_filter",
    "all",
    isMediaFilter,
  );
  const _sortStore = createFilterState<SortBy>(
    "duplicates_sort_by",
    "title",
    isSortBy,
  );
  const _crossStore = createFilterState<boolean>(
    "duplicates_cross_library",
    false,
    isBool,
  );
  const _manualStore = createFilterState<boolean>(
    "duplicates_show_manual",
    true,
    isBool,
  );
  const _ignoredStore = createFilterState<boolean>(
    "duplicates_show_ignored",
    false,
    isBool,
  );
  const _perPageStore = createPerPageState("duplicates_per_page");

  let data = $state<PaginatedDuplicatesResponse | null>(null);
  let loading = $state(true);
  let error = $state("");
  let searchQuery = $state("");
  let mediaFilter = $state<MediaFilter>(_mediaStore.getInitial());
  let sortBy = $state<SortBy>(_sortStore.getInitial());
  let includeCrossLibrary = $state(_crossStore.getInitial());
  let includeManual = $state(_manualStore.getInitial());
  let includeIgnored = $state(_ignoredStore.getInitial());
  let perPage = $state(_perPageStore.getInitial());
  let currentPage = $state(1);
  let mounted = $state(false);

  // group key -> index of the file to keep (defaults to 0, the suggestion)
  let keepers = $state<Record<string, number>>({});
  let selectedKeys = $state<Set<string>>(new Set());

  let confirmOpen = $state(false);
  let confirmGroups = $state<DuplicateGroup[]>([]);
  let submitting = $state(false);

  let settingsOpen = $state(false);
  let priority = $state<DuplicateKeeperPriorityEntry[]>([]);
  let savingSettings = $state(false);

  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  let abortController: AbortController | null = null;

  const isAdmin = $derived($auth.user?.role === UserRole.Admin);
  const canManage = $derived(
    isAdmin ||
      ($auth.user?.permissions ?? []).includes(Permission.ManageReclaim),
  );

  const groups = $derived(data?.items ?? []);

  $effect(() => _mediaStore.save(mediaFilter));
  $effect(() => _sortStore.save(sortBy));
  $effect(() => _crossStore.save(includeCrossLibrary));
  $effect(() => _manualStore.save(includeManual));
  $effect(() => _ignoredStore.save(includeIgnored));

  // reload from page 1 whenever a filter changes
  $effect(() => {
    mediaFilter;
    sortBy;
    includeCrossLibrary;
    includeManual;
    includeIgnored;
    perPage;
    if (mounted) void load(1);
  });

  onMount(() => {
    mounted = true;
  });

  const load = async (page: number = currentPage) => {
    abortController?.abort();
    abortController = new AbortController();
    const signal = abortController.signal;
    loading = true;
    error = "";
    currentPage = page;
    selectedKeys = new Set();
    try {
      const params = new URLSearchParams({
        page: page.toString(),
        per_page: perPage.toString(),
        sort_by: sortBy,
        include_cross_library: String(includeCrossLibrary),
        include_manual: String(includeManual),
        include_ignored: String(includeIgnored),
      });
      if (searchQuery.trim()) params.append("search", searchQuery.trim());
      if (mediaFilter !== "all") params.append("media_type", mediaFilter);
      const response = await get_api<PaginatedDuplicatesResponse>(
        `/api/duplicates?${params.toString()}`,
        signal,
      );
      if (response.total_pages > 0 && page > response.total_pages) {
        await load(response.total_pages);
        return;
      }
      data = response;
      keepers = {};
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      error = e.message ?? "Failed to load duplicates.";
    } finally {
      if (!signal.aborted) loading = false;
    }
  };

  const handleSearch = (event: Event) => {
    searchQuery = (event.target as HTMLInputElement).value;
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => load(1), 400);
  };

  // ---- per group helpers ----

  const keeperIndex = (group: DuplicateGroup) => keepers[group.key] ?? 0;

  const isActionable = (group: DuplicateGroup) =>
    !group.manual_reason && !group.ignored;

  /** Files removed if this group is cleaned up: everything but the keeper, minus protected files. */
  const filesToDelete = (group: DuplicateGroup): DuplicateFile[] =>
    group.files.filter((f, i) => i !== keeperIndex(group) && !f.protected);

  const deleteBytes = (group: DuplicateGroup) =>
    filesToDelete(group).reduce((sum, f) => sum + f.size, 0);

  const canClean = (group: DuplicateGroup) =>
    canManage && isActionable(group) && filesToDelete(group).length > 0;

  const groupLabel = (group: DuplicateGroup) => {
    if (group.media_type === MediaType.Movie) {
      return group.year ? `${group.title} (${group.year})` : group.title;
    }
    const s = String(group.season_number ?? 0).padStart(2, "0");
    const e = String(group.episode_number ?? 0).padStart(2, "0");
    return `${group.title} S${s}E${e}`;
  };

  const fileChips = (f: DuplicateFile): string[] => {
    const chips: string[] = [];
    if (f.video_width && f.video_height) {
      chips.push(`${f.video_width}x${f.video_height}`);
    } else if (f.video_resolution) {
      chips.push(f.video_resolution);
    }
    if (f.video_codec_family) chips.push(f.video_codec_family.toUpperCase());
    if (f.video_dolby_vision) chips.push("DV");
    else if (f.video_hdr) chips.push("HDR");
    if (f.audio_codec_family) {
      chips.push(
        f.audio_channels
          ? `${f.audio_codec_family.toUpperCase()} ${f.audio_channels}ch`
          : f.audio_codec_family.toUpperCase(),
      );
    }
    if (f.video_bitrate) {
      // Plex reports kbps; Jellyfin/Emby report bps
      const kbps =
        f.video_bitrate > 1_000_000 ? f.video_bitrate / 1000 : f.video_bitrate;
      chips.push(`${(kbps / 1000).toFixed(1)} Mbps`);
    }
    return chips;
  };

  const fileName = (path: string | null) =>
    path ? (path.split(/[\\/]/).pop() ?? path) : "Unknown path";

  const folderName = (path: string | null) => {
    if (!path) return "";
    const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
    return cut > 0 ? path.slice(0, cut) : "";
  };

  // ---- selection ----

  const selectableOnPage = $derived(groups.filter(canClean));
  const allPageSelected = $derived(
    selectableOnPage.length > 0 &&
      selectableOnPage.every((g) => selectedKeys.has(g.key)),
  );
  const somePageSelected = $derived(
    !allPageSelected && selectableOnPage.some((g) => selectedKeys.has(g.key)),
  );
  const selectedGroups = $derived(
    groups.filter((g) => selectedKeys.has(g.key) && canClean(g)),
  );
  const selectedBytes = $derived(
    selectedGroups.reduce((sum, g) => sum + deleteBytes(g), 0),
  );

  const toggleSelect = (key: string) => {
    const next = new Set(selectedKeys);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    selectedKeys = next;
  };

  const toggleSelectAll = () => {
    selectedKeys = allPageSelected
      ? new Set()
      : new Set(selectableOnPage.map((g) => g.key));
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
      toast.error(job.error_message ?? "Duplicate cleanup did not finish.");
      return;
    }
    const result = ((job.payload ?? {}) as Record<string, unknown>).result as
      | DuplicateJobResult
      | undefined;
    if (!result) return;
    if ((result.succeeded ?? 0) > 0) {
      toast.success(
        `Cleaned up ${result.succeeded} item${result.succeeded === 1 ? "" : "s"}, freed ${formatFileSize(result.freed_bytes ?? 0)}.`,
      );
    }
    if ((result.failed ?? 0) > 0) {
      const reasons = (result.errors ?? []).slice(0, 3).join("\n");
      toast.error(
        `${result.failed} item${result.failed === 1 ? "" : "s"} could not be cleaned up.${reasons ? `\n${reasons}` : ""}`,
      );
    }
  };

  const openConfirm = (targets: DuplicateGroup[]) => {
    confirmGroups = targets.filter(canClean);
    if (confirmGroups.length) confirmOpen = true;
  };

  const confirmBytes = $derived(
    confirmGroups.reduce((sum, g) => sum + deleteBytes(g), 0),
  );
  const confirmFileCount = $derived(
    confirmGroups.reduce((sum, g) => sum + filesToDelete(g).length, 0),
  );

  const submitDelete = async () => {
    submitting = true;
    try {
      const response = await post_api<{ message: string; job_id: number }>(
        "/api/duplicates/delete",
        {
          items: confirmGroups.map((g) => ({
            media_type: g.media_type,
            item_id: g.item_id,
            version_ids: filesToDelete(g).flatMap((f) => f.version_ids),
          })),
        },
      );
      toast.success(response.message);
      confirmOpen = false;
      selectedKeys = new Set();
      void watchJob(response.job_id).then(async (job) => {
        reportJob(job);
        await load(currentPage);
        uiIndicators.invalidate();
      });
    } catch (e: any) {
      toast.error(e.message ?? "Failed to queue duplicate cleanup.");
    } finally {
      submitting = false;
    }
  };

  const setIgnored = async (group: DuplicateGroup, ignored: boolean) => {
    try {
      await post_api(
        ignored ? "/api/duplicates/ignore" : "/api/duplicates/unignore",
        { media_type: group.media_type, item_id: group.item_id },
      );
      toast.success(
        ignored ? "Marked as not a duplicate" : "Duplicate restored",
      );
      await load(currentPage);
    } catch (e: any) {
      toast.error(e.message ?? "Failed to update duplicate.");
    }
  };

  // ---- keeper priority settings (admin) ----

  const openSettings = async () => {
    try {
      const response = await get_api<{
        keeper_priority: DuplicateKeeperPriorityEntry[];
      }>("/api/duplicates/settings");
      priority = response.keeper_priority;
      settingsOpen = true;
    } catch (e: any) {
      toast.error(e.message ?? "Failed to load keeper preference.");
    }
  };

  const movePriority = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= priority.length) return;
    const next = [...priority];
    [next[index], next[target]] = [next[target], next[index]];
    priority = next;
  };

  const togglePriority = (index: number, enabled: boolean) => {
    const key = priority[index].key;
    priority = priority.map((entry, i) => {
      if (i === index) return { ...entry, enabled };
      // larger and smaller file cannot both apply
      if (
        enabled &&
        ((key === "size_larger" && entry.key === "size_smaller") ||
          (key === "size_smaller" && entry.key === "size_larger"))
      ) {
        return { ...entry, enabled: false };
      }
      return entry;
    });
  };

  const saveSettings = async () => {
    savingSettings = true;
    try {
      await put_api("/api/duplicates/settings", { keeper_priority: priority });
      toast.success("Keeper preference saved");
      settingsOpen = false;
      await load(currentPage);
    } catch (e: any) {
      toast.error(e.message ?? "Failed to save keeper preference.");
    } finally {
      savingSettings = false;
    }
  };
</script>

<AlertDialog.Root bind:open={confirmOpen}>
  <AlertDialog.Content class="text-foreground">
    <AlertDialog.Header>
      <AlertDialog.Title>
        Delete {confirmFileCount} file{confirmFileCount === 1 ? "" : "s"}?
      </AlertDialog.Title>
      <AlertDialog.Description>
        {confirmGroups.length === 1
          ? groupLabel(confirmGroups[0])
          : `${confirmGroups.length} items`} keep{confirmGroups.length === 1
          ? "s"
          : ""} the file you picked. The other copies ({formatFileSize(
          confirmBytes,
        )}) are removed from disk through Radarr/Sonarr, or the media server
        when the Arr does not track them. Nothing is unmonitored. This cannot be
        undone.
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

<Dialog.Root bind:open={settingsOpen}>
  <Dialog.Content class="text-foreground sm:max-w-lg">
    <Dialog.Header>
      <Dialog.Title>Keeper preference</Dialog.Title>
      <Dialog.Description>
        Picks the suggested file to keep. Checked in order; the first rule that
        tells two files apart wins. Ties go to the newest file.
      </Dialog.Description>
    </Dialog.Header>
    <ol class="space-y-1.5">
      {#each priority as entry, index (entry.key)}
        <li
          class="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2"
        >
          <span class="w-5 text-xs text-muted-foreground">{index + 1}</span>
          <span
            class="flex-1 text-sm {entry.enabled
              ? 'text-foreground'
              : 'text-muted-foreground line-through'}"
          >
            {CRITERION_LABELS[entry.key] ?? entry.key}
          </span>
          <Button
            size="icon"
            variant="ghost"
            class="size-7 cursor-pointer"
            disabled={index === 0}
            onclick={() => movePriority(index, -1)}
            aria-label="Move up"
          >
            <ArrowUp class="size-4" />
          </Button>
          <Button
            size="icon"
            variant="ghost"
            class="size-7 cursor-pointer"
            disabled={index === priority.length - 1}
            onclick={() => movePriority(index, 1)}
            aria-label="Move down"
          >
            <ArrowDown class="size-4" />
          </Button>
          <Switch
            checked={entry.enabled}
            onCheckedChange={(v) => togglePriority(index, v)}
            aria-label="Use this rule"
          />
        </li>
      {/each}
    </ol>
    <Dialog.Footer>
      <Button
        variant="outline"
        onclick={() => (settingsOpen = false)}
        disabled={savingSettings}
      >
        Cancel
      </Button>
      <Button onclick={saveSettings} disabled={savingSettings}>
        {savingSettings ? "Saving..." : "Save"}
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

<div class="p-2.5 md:p-8">
  <div class="max-w-7xl mx-auto space-y-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 class="text-3xl font-bold text-foreground">Duplicates</h1>
        <p class="text-muted-foreground">
          Movies and episodes your media server holds more than one file for.
          Pick the file to keep; the rest can be removed.
        </p>
      </div>
      {#if isAdmin}
        <Button variant="outline" class="cursor-pointer" onclick={openSettings}>
          <SlidersHorizontal class="size-4" />
          Keeper preference
        </Button>
      {/if}
    </div>

    {#if data && data.summary.groups > 0}
      <div class="grid grid-cols-3 gap-2">
        <div class="rounded-lg border border-border bg-card px-4 py-3">
          <p class="text-xs text-muted-foreground">Duplicate groups</p>
          <p class="text-xl font-semibold text-foreground">
            {data.summary.groups}
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
          value={mediaFilter}
          onValueChange={(v) => {
            if (isMediaFilter(v)) mediaFilter = v;
          }}
        >
          <Select.Trigger class="flex-1 bg-card text-card-foreground">
            {mediaFilter === "all"
              ? "All media"
              : mediaFilter === MediaType.Movie
                ? "Movies"
                : "Episodes"}
          </Select.Trigger>
          <Select.Content class="bg-card">
            <Select.Item
              value="all"
              label="All media"
              class="text-card-foreground"
            >
              All media
            </Select.Item>
            <Select.Item
              value={MediaType.Movie}
              label="Movies"
              class="text-card-foreground"
            >
              Movies
            </Select.Item>
            <Select.Item
              value={MediaType.Series}
              label="Episodes"
              class="text-card-foreground"
            >
              Episodes
            </Select.Item>
          </Select.Content>
        </Select.Root>
        <Select.Root
          type="single"
          value={sortBy}
          onValueChange={(v) => {
            if (isSortBy(v)) sortBy = v;
          }}
        >
          <Select.Trigger class="flex-1 bg-card text-card-foreground">
            {sortBy === "title" ? "Title" : "Most reclaimable"}
          </Select.Trigger>
          <Select.Content class="bg-card">
            <Select.Item
              value="title"
              label="Title"
              class="text-card-foreground"
            >
              Title
            </Select.Item>
            <Select.Item
              value="size"
              label="Most reclaimable"
              class="text-card-foreground"
            >
              Most reclaimable
            </Select.Item>
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
        <Switch bind:checked={includeCrossLibrary} />
        Include copies in different libraries
      </label>
      <label class="flex items-center gap-2 cursor-pointer">
        <Switch bind:checked={includeManual} />
        Show items that need manual review
      </label>
      <label class="flex items-center gap-2 cursor-pointer">
        <Switch bind:checked={includeIgnored} />
        Show "not a duplicate"
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
          {selectedGroups.length} of {selectableOnPage.length} selected
        </span>
      </div>
    {/if}

    {#if canManage && selectedGroups.length > 0}
      <div
        class="flex items-center justify-between gap-4 px-4 py-3 bg-primary/10 border border-primary/30 rounded-lg"
      >
        <span class="text-sm text-foreground font-medium">
          {selectedGroups.length} item{selectedGroups.length === 1 ? "" : "s"} selected
          <span class="text-muted-foreground font-normal">
            - {formatFileSize(selectedBytes)} reclaimable
          </span>
        </span>
        <Button
          size="sm"
          variant="destructive"
          class="cursor-pointer"
          onclick={() => openConfirm(selectedGroups)}
        >
          <Trash2 class="size-4" />
          Delete other copies
        </Button>
      </div>
    {/if}

    <ErrorBox {error} />

    <div class="bg-card rounded-lg border border-border">
      {#if loading}
        <div class="p-8 text-center text-muted-foreground">
          <div
            class="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-primary border-r-transparent"
          ></div>
          <p class="mt-4">Loading duplicates...</p>
        </div>
      {:else if groups.length === 0}
        <div class="p-8 text-center text-muted-foreground">
          No duplicates found.
          {#if !includeCrossLibrary}
            <span class="block text-sm mt-1">
              Copies in different libraries (for example a separate 4K library)
              are hidden by default.
            </span>
          {/if}
        </div>
      {:else}
        <ul class="divide-y divide-border">
          {#each groups as group (group.key)}
            <li class="p-3 md:p-4 space-y-3">
              <div class="flex items-start gap-3">
                {#if canManage}
                  <input
                    type="checkbox"
                    class="mt-1 cursor-pointer accent-primary disabled:cursor-not-allowed"
                    checked={selectedKeys.has(group.key)}
                    disabled={!canClean(group)}
                    onchange={() => toggleSelect(group.key)}
                    aria-label="Select {groupLabel(group)}"
                  />
                {/if}
                <PosterThumb
                  mediaType={group.media_type}
                  posterUrl={group.poster_url}
                  tailWindElSize="w-12"
                  showMediaType
                />
                <div class="flex-1 min-w-0">
                  <p class="font-semibold text-foreground truncate">
                    {groupLabel(group)}
                  </p>
                  {#if group.episode_name}
                    <p class="text-sm text-muted-foreground truncate">
                      {group.episode_name}
                    </p>
                  {/if}
                  <div class="flex flex-wrap gap-1.5 mt-1 text-xs">
                    <span class="text-muted-foreground">
                      {group.files.length} files
                    </span>
                    {#if group.cross_library}
                      <span
                        class="px-2 rounded-full border border-border text-muted-foreground"
                      >
                        Different libraries
                      </span>
                    {/if}
                    {#if group.ignored}
                      <span
                        class="px-2 rounded-full border border-border text-muted-foreground"
                      >
                        Not a duplicate
                      </span>
                    {/if}
                  </div>
                  {#if group.manual_reason}
                    <p
                      class="mt-1.5 flex items-center gap-1.5 text-sm text-amber-600 dark:text-amber-400"
                    >
                      <TriangleAlert class="size-4 shrink-0" />
                      Manual review: {group.manual_reason}
                    </p>
                  {/if}
                </div>
                {#if canManage}
                  <div class="flex flex-wrap justify-end gap-2">
                    {#if group.ignored}
                      <Button
                        size="sm"
                        variant="outline"
                        class="cursor-pointer"
                        onclick={() => setIgnored(group, false)}
                      >
                        <Eye class="size-4" />
                        Restore
                      </Button>
                    {:else}
                      <Button
                        size="sm"
                        variant="outline"
                        class="cursor-pointer"
                        onclick={() => setIgnored(group, true)}
                      >
                        <EyeOff class="size-4" />
                        Not a duplicate
                      </Button>
                    {/if}
                    <Button
                      size="sm"
                      variant="destructive"
                      class="cursor-pointer"
                      disabled={!canClean(group)}
                      onclick={() => openConfirm([group])}
                    >
                      <Trash2 class="size-4" />
                      Delete others ({formatFileSize(deleteBytes(group))})
                    </Button>
                  </div>
                {/if}
              </div>

              <div class="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                {#each group.files as file, index (file.version_ids[0])}
                  {@const isKeeper = index === keeperIndex(group)}
                  <label
                    class="rounded-md border px-3 py-2 text-sm transition-colors
                      {isKeeper
                      ? 'border-primary bg-primary/10'
                      : 'border-border bg-muted/30'}
                      {canManage && isActionable(group)
                      ? 'cursor-pointer'
                      : ''}"
                  >
                    <div class="flex items-center gap-2">
                      {#if canManage}
                        <input
                          type="radio"
                          name="keep-{group.key}"
                          class="accent-primary"
                          checked={isKeeper}
                          disabled={!isActionable(group)}
                          onchange={() =>
                            (keepers = { ...keepers, [group.key]: index })}
                        />
                      {/if}
                      <span class="font-medium text-foreground">
                        {isKeeper ? "Keep" : "Remove"}
                      </span>
                      {#if index === 0}
                        <span class="text-xs text-muted-foreground"
                          >(suggested)</span
                        >
                      {/if}
                      <span class="ml-auto flex items-center gap-2">
                        {#if file.protected}
                          <span
                            class="flex items-center gap-1 text-xs text-muted-foreground"
                            title="Protected files are never deleted"
                          >
                            <Lock class="size-3.5" /> Protected
                          </span>
                        {/if}
                        <span class="font-medium text-foreground">
                          {formatFileSize(file.size)}
                        </span>
                      </span>
                    </div>
                    <div class="flex flex-wrap gap-1 mt-1.5">
                      {#each fileChips(file) as chip}
                        <span
                          class="text-xs leading-5 px-2 rounded-full border border-border text-muted-foreground"
                        >
                          {chip}
                        </span>
                      {/each}
                    </div>
                    <p class="mt-1.5 text-xs text-foreground break-all">
                      {fileName(file.path)}
                    </p>
                    {#if folderName(file.path)}
                      <p
                        class="text-xs font-mono text-muted-foreground break-all"
                      >
                        {folderName(file.path)}
                      </p>
                    {/if}
                    <p class="mt-1 text-xs text-muted-foreground">
                      {file.library_names.join(", ")}
                    </p>
                  </label>
                {/each}
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </div>

    {#if !loading && data && data.total_pages > 1}
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
</div>
