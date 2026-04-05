<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import ErrorBox from "$lib/components/error-box.svelte";
  import CompactPagination from "$lib/components/compact-pagination.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { auth } from "$lib/stores/auth";
  import {
    MediaType,
    UserRole,
    Permission,
    type ReclaimCandidateEntry,
    type ProtectionRequest,
    type PaginatedResponse,
  } from "$lib/types/shared";

  interface DeleteResponse {
    deleted: number;
    failed: number;
  }
  import { formatDate } from "$lib/utils/date";
  import { toast } from "svelte-sonner";
  import Search from "@lucide/svelte/icons/search";
  import Trash from "@lucide/svelte/icons/trash";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import ProtectionRequestDialog from "$lib/components/media/protection-request-dialog.svelte";
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import ShieldBan from "@lucide/svelte/icons/shield-ban";

  // display row (either a flat entry or a season group)
  type FlatRow = { kind: "flat"; entry: ReclaimCandidateEntry };
  type GroupRow = {
    kind: "group";
    // series level candidate if the whole series also has one (may be null)
    seriesEntry: ReclaimCandidateEntry | null;
    seasons: ReclaimCandidateEntry[];
    // from the first season entry
    media_id: number;
    media_title: string;
    media_year: number | null;
    poster_url: string | null;
  };
  type DisplayRow = FlatRow | GroupRow;

  // state
  let data = $state<PaginatedResponse<ReclaimCandidateEntry> | null>(null);
  let loading = $state(true);
  let error = $state("");
  let searchQuery = $state("");
  let sortBy = $state("created_at");
  let sortOrder = $state("desc");
  let mediaTypeFilter = $state("all");
  let currentPage = $state(1);
  const PER_PAGE = 25;
  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  let abortController: AbortController | null = null;
  let mounted = $state(false);

  // which group rows are expanded (keyed by media_id)
  let expandedGroups = $state<Set<number>>(new Set());

  // selection (admins only - stores individual candidate IDs)
  let selectedIds = $state<Set<number>>(new Set());

  // single-item request dialog
  let requestDialogOpen = $state(false);
  let requestTarget = $state<ReclaimCandidateEntry | null>(null);

  // single-item delete confirmation
  let deleteDialogOpen = $state(false);
  let deleteTarget = $state<ReclaimCandidateEntry | null>(null);
  let deleteSubmitting = $state(false);

  // bulk request dialog
  let bulkDialogOpen = $state(false);
  let bulkDuration = $state("permanent");
  let bulkCustomDays = $state("30");
  let bulkSubmitting = $state(false);

  // bulk delete confirmation
  let bulkDeleteDialogOpen = $state(false);
  let bulkDeleteSubmitting = $state(false);

  const sortByOptions = [
    { value: "created_at", label: "Date Added" },
    { value: "media_title", label: "Title" },
    { value: "estimated_space_gb", label: "Space" },
  ];

  const durationOptions = [
    { value: "30", label: "30 days" },
    { value: "90", label: "90 days" },
    { value: "180", label: "180 days" },
    { value: "365", label: "1 year" },
    { value: "custom", label: "Custom days" },
    { value: "permanent", label: "Permanent" },
  ];

  const entries = $derived(data?.items ?? []);

  // build grouped display rows from the flat API response
  //
  // season candidates (season_id != null) with the same media_id are collapsed
  // into a single GroupRow. everything else stays flat.
  const displayRows = $derived((): DisplayRow[] => {
    // collect season candidates by series media_id
    const seasonGroups = new Map<number, ReclaimCandidateEntry[]>();
    // collect whole-series candidates by media_id (season_id == null, type == series)
    const seriesFlat = new Map<number, ReclaimCandidateEntry>();
    const flatRows: DisplayRow[] = [];

    for (const e of entries) {
      if (e.season_id != null) {
        const g = seasonGroups.get(e.media_id) ?? [];
        g.push(e);
        seasonGroups.set(e.media_id, g);
      } else if (e.media_type === MediaType.Series) {
        seriesFlat.set(e.media_id, e);
      } else {
        // plain movie (always flat)
        flatRows.push({ kind: "flat", entry: e });
      }
    }

    // whole series candidates (if there are also season candidates for the same
    // series, merge them into a group; otherwise stay flat)
    for (const [mid, fe] of seriesFlat) {
      if (seasonGroups.has(mid)) {
        // will be merged below
      } else {
        flatRows.push({ kind: "flat", entry: fe });
      }
    }

    const result: DisplayRow[] = [...flatRows];

    for (const [mid, seasons] of seasonGroups) {
      const sorted = [...seasons].sort(
        (a, b) => (a.season_number ?? 0) - (b.season_number ?? 0),
      );
      const first = sorted[0];
      result.push({
        kind: "group",
        seriesEntry: seriesFlat.get(mid) ?? null,
        seasons: sorted,
        media_id: mid,
        media_title: first.series_title ?? first.media_title,
        media_year: first.media_year,
        poster_url: first.poster_url,
      });
    }

    return result;
  });

  const isAdmin = $derived(
    $auth.user?.role === UserRole.Admin ||
      ($auth.user?.permissions ?? []).includes(Permission.AutoApprove),
  );

  const canDelete = $derived(
    $auth.user?.role === UserRole.Admin ||
      ($auth.user?.permissions ?? []).includes(Permission.ManageReclaim),
  );

  const canBulkSelect = $derived(isAdmin || canDelete);

  // all selectable IDs on the current page (flat entries + all season sub entries)
  const allSelectableIds = $derived((): number[] => {
    const ids: number[] = [];
    for (const row of displayRows()) {
      if (row.kind === "flat") ids.push(row.entry.id);
      else {
        if (row.seriesEntry) ids.push(row.seriesEntry.id);
        for (const s of row.seasons) ids.push(s.id);
      }
    }
    return ids;
  });

  const allPageSelected = $derived(
    allSelectableIds().length > 0 &&
      allSelectableIds().every((id) => selectedIds.has(id)),
  );

  const selectedEntries = $derived(
    entries.filter((e) => selectedIds.has(e.id)),
  );

  $effect(() => {
    sortBy;
    sortOrder;
    mediaTypeFilter;
    if (mounted) loadCandidates(1);
  });

  const loadCandidates = async (page: number = currentPage) => {
    if (abortController) abortController.abort();
    abortController = new AbortController();
    const signal = abortController.signal;

    loading = true;
    error = "";
    currentPage = page;
    selectedIds = new Set();
    expandedGroups = new Set();

    try {
      const params = new URLSearchParams({
        page: page.toString(),
        per_page: PER_PAGE.toString(),
        sort_by: sortBy,
        sort_order: sortOrder,
      });

      if (searchQuery.trim()) params.append("search", searchQuery.trim());
      if (mediaTypeFilter !== "all")
        params.append("media_type", mediaTypeFilter);

      data = await get_api<PaginatedResponse<ReclaimCandidateEntry>>(
        `/api/media/candidates?${params.toString()}`,
        signal,
      );
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      error = e.message ?? "Failed to load candidates.";
    } finally {
      if (!signal.aborted) loading = false;
    }
  };

  const handleSearch = (event: Event) => {
    searchQuery = (event.target as HTMLInputElement).value;
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadCandidates(1), 400);
  };

  const toggleSelect = (id: number) => {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedIds = next;
  };

  // toggle all seasons in a group
  const toggleGroupSelect = (row: GroupRow) => {
    const groupIds = row.seasons.map((s) => s.id);
    if (row.seriesEntry) groupIds.push(row.seriesEntry.id);
    const allSelected = groupIds.every((id) => selectedIds.has(id));
    const next = new Set(selectedIds);
    if (allSelected) groupIds.forEach((id) => next.delete(id));
    else groupIds.forEach((id) => next.add(id));
    selectedIds = next;
  };

  // a group is fully selected if all season entries + the series entry (if exists) are selected
  const isGroupAllSelected = (row: GroupRow): boolean => {
    const groupIds = row.seasons.map((s) => s.id);
    if (row.seriesEntry) groupIds.push(row.seriesEntry.id);
    return groupIds.length > 0 && groupIds.every((id) => selectedIds.has(id));
  };

  // a group is partially selected if some (but not all) season entries or the series entry are selected
  const isGroupPartialSelected = (row: GroupRow): boolean => {
    const groupIds = row.seasons.map((s) => s.id);
    if (row.seriesEntry) groupIds.push(row.seriesEntry.id);
    const someSelected = groupIds.some((id) => selectedIds.has(id));
    return someSelected && !isGroupAllSelected(row);
  };

  // toggle select all on the current page (flat entries + all season sub entries)
  const toggleSelectAll = () => {
    const ids = allSelectableIds();
    if (allPageSelected) {
      selectedIds = new Set();
    } else {
      selectedIds = new Set(ids);
    }
  };

  // toggle expand/collapse of a season group
  const toggleExpand = (mediaId: number) => {
    const next = new Set(expandedGroups);
    if (next.has(mediaId)) next.delete(mediaId);
    else next.add(mediaId);
    expandedGroups = next;
  };

  const openSingleRequest = (entry: ReclaimCandidateEntry) => {
    requestTarget = entry;
    requestDialogOpen = true;
  };

  const openSingleDelete = (entry: ReclaimCandidateEntry) => {
    deleteTarget = entry;
    deleteDialogOpen = true;
  };

  const removeCandidateIds = (ids: Set<number>) => {
    if (!data) return;
    const remaining = data.items.filter((i) => !ids.has(i.id));
    const removed = data.items.length - remaining.length;
    const newTotal = Math.max(0, data.total - removed);
    data = {
      ...data,
      items: remaining,
      total: newTotal,
      total_pages: newTotal > 0 ? Math.ceil(newTotal / data.per_page) : 0,
    };
  };

  const submitSingleDelete = async () => {
    if (!deleteTarget) return;
    deleteSubmitting = true;
    try {
      const resp = await post_api<DeleteResponse>(
        "/api/media/candidates/delete",
        { candidate_ids: [deleteTarget.id] },
      );
      if (resp.deleted > 0) {
        const label =
          deleteTarget.season_number != null
            ? `S${String(deleteTarget.season_number).padStart(2, "0")} of ` +
              `${deleteTarget.series_title ?? deleteTarget.media_title}`
            : deleteTarget.media_title;
        toast.success(`Deleted ${label}.`);
        const remaining = data?.items.filter((i) => i.id !== deleteTarget!.id);
        if (!remaining || (remaining.length === 0 && currentPage > 1)) {
          await loadCandidates(currentPage - 1);
        } else {
          removeCandidateIds(new Set([deleteTarget.id]));
        }
      } else {
        toast.error(`Failed to delete.`);
      }
      deleteDialogOpen = false;
      deleteTarget = null;
    } catch (e: any) {
      toast.error(e.message ?? "Failed to delete candidate.");
    } finally {
      deleteSubmitting = false;
    }
  };

  const submitBulkDelete = async () => {
    if (selectedEntries.length === 0) return;
    bulkDeleteSubmitting = true;
    try {
      const resp = await post_api<DeleteResponse>(
        "/api/media/candidates/delete",
        { candidate_ids: selectedEntries.map((e) => e.id) },
      );
      if (resp.deleted > 0)
        toast.success(
          `Deleted ${resp.deleted} item${resp.deleted !== 1 ? "s" : ""}.`,
        );
      if (resp.failed > 0)
        toast.error(
          `${resp.failed} item${resp.failed !== 1 ? "s" : ""} could not be deleted.`,
        );
      if (resp.deleted > 0) await loadCandidates(currentPage);
    } catch (e: any) {
      toast.error(e.message ?? "Failed to delete candidates.");
    } finally {
      bulkDeleteSubmitting = false;
      bulkDeleteDialogOpen = false;
      selectedIds = new Set();
    }
  };

  const handleRequestSuccess = (request: ProtectionRequest) => {
    if (!data) return;
    data = {
      ...data,
      items: data.items.map((item) =>
        item.media_id === request.media_id &&
        item.media_type === request.media_type
          ? { ...item, has_pending_request: true }
          : item,
      ),
    };
  };

  const submitBulkRequest = async () => {
    if (selectedEntries.length === 0) return;
    bulkSubmitting = true;

    let durationDays: number | null = null;
    if (bulkDuration === "permanent") {
      durationDays = null;
    } else if (bulkDuration === "custom") {
      const parsed = Number(bulkCustomDays);
      if (!Number.isInteger(parsed) || parsed <= 0) {
        toast.error("Custom duration must be a positive whole number of days");
        bulkSubmitting = false;
        return;
      }
      durationDays = parsed;
    } else {
      durationDays = Number(bulkDuration);
    }

    const results = await Promise.allSettled(
      selectedEntries.map((entry) =>
        post_api<ProtectionRequest>("/api/protection-requests", {
          media_type: entry.media_type,
          media_id: entry.media_id,
          season_id: entry.season_id ?? undefined,
          reason: "Admin decision",
          duration_days: durationDays,
        }),
      ),
    );

    const succeeded = results.filter((r) => r.status === "fulfilled").length;
    const failed = results.length - succeeded;

    if (succeeded > 0)
      toast.success(
        `Submitted ${succeeded} protection request${succeeded !== 1 ? "s" : ""}`,
      );
    if (failed > 0)
      toast.error(`${failed} request${failed !== 1 ? "s" : ""} failed`);

    selectedIds = new Set();
    bulkDialogOpen = false;
    bulkSubmitting = false;
  };

  const toMediaLike = (entry: ReclaimCandidateEntry) => ({
    id: entry.media_id,
    title: entry.series_title ?? entry.media_title,
    year: entry.media_year,
    poster_url: entry.poster_url,
    status: { is_candidate: true },
  });

  const spaceLabel = (gb: number | null) =>
    gb != null ? `${gb.toFixed(2)} GB` : "?";

  const groupTotalGb = (row: GroupRow): number =>
    row.seasons.reduce((acc, s) => acc + (s.estimated_space_gb ?? 0), 0) +
    (row.seriesEntry?.estimated_space_gb ?? 0);

  onMount(async () => {
    mounted = true;
    await loadCandidates();
  });

  onDestroy(() => {
    if (searchTimer) clearTimeout(searchTimer);
    if (abortController) abortController.abort();
  });
</script>

<!-- bulk delete confirmation -->
<AlertDialog.Root bind:open={bulkDeleteDialogOpen}>
  <AlertDialog.Content class="text-foreground">
    <AlertDialog.Header>
      <AlertDialog.Title
        >Delete {selectedEntries.length} Item{selectedEntries.length !== 1
          ? "s"
          : ""}?</AlertDialog.Title
      >
      <AlertDialog.Description>
        This will permanently delete {selectedEntries.length} item{selectedEntries.length !==
        1
          ? "s"
          : ""} from all configured services and remove the files from disk. This
        cannot be undone.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel disabled={bulkDeleteSubmitting}
        >Cancel</AlertDialog.Cancel
      >
      <AlertDialog.Action
        onclick={submitBulkDelete}
        disabled={bulkDeleteSubmitting}
      >
        {bulkDeleteSubmitting
          ? "Deleting..."
          : `Delete ${selectedEntries.length}`}
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<!-- single delete confirmation -->
<AlertDialog.Root bind:open={deleteDialogOpen}>
  <AlertDialog.Content class="text-foreground">
    <AlertDialog.Header>
      <AlertDialog.Title>Delete Permanently?</AlertDialog.Title>
      <AlertDialog.Description>
        {#if deleteTarget}
          {#if deleteTarget.season_number != null}
            Permanently delete <strong
              >Season {deleteTarget.season_number} of {deleteTarget.series_title ??
                deleteTarget.media_title}</strong
            > from all configured services and remove the files from disk. This cannot
            be undone.
          {:else}
            Permanently delete <strong>{deleteTarget.media_title}</strong> from all
            configured services and remove the files from disk. This cannot be undone.
          {/if}
        {/if}
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel disabled={deleteSubmitting}>Cancel</AlertDialog.Cancel
      >
      <AlertDialog.Action
        onclick={submitSingleDelete}
        disabled={deleteSubmitting}
      >
        {deleteSubmitting ? "Deleting..." : "Delete"}
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<!-- bulk request dialog -->
<Dialog.Root bind:open={bulkDialogOpen}>
  <Dialog.Content class="sm:max-w-md text-foreground">
    <Dialog.Header>
      <Dialog.Title
        >Request Exception for {selectedEntries.length} Items</Dialog.Title
      >
      <Dialog.Description>
        All selected candidates will be submitted as protection requests and
        auto-approved.
      </Dialog.Description>
    </Dialog.Header>

    <div class="flex flex-col gap-4 py-2">
      <div class="flex flex-col gap-2">
        <Label>Protection duration</Label>
        <Select.Root type="single" bind:value={bulkDuration}>
          <Select.Trigger class="w-full">
            {durationOptions.find((o) => o.value === bulkDuration)?.label}
          </Select.Trigger>
          <Select.Content class="bg-card border-ring">
            {#each durationOptions as opt}
              <Select.Item
                value={opt.value}
                label={opt.label}
                class="text-foreground"
              >
                {opt.label}
              </Select.Item>
            {/each}
          </Select.Content>
        </Select.Root>
        {#if bulkDuration === "custom"}
          <Input
            type="number"
            min={1}
            step={1}
            bind:value={bulkCustomDays}
            placeholder="Enter number of days"
            class="input-hover-el"
            disabled={bulkSubmitting}
          />
        {/if}
      </div>
    </div>

    <Dialog.Footer>
      <Button
        variant="outline"
        onclick={() => (bulkDialogOpen = false)}
        disabled={bulkSubmitting}
      >
        Cancel
      </Button>
      <Button onclick={submitBulkRequest} disabled={bulkSubmitting}>
        {bulkSubmitting
          ? "Submitting..."
          : `Submit ${selectedEntries.length} Request${selectedEntries.length !== 1 ? "s" : ""}`}
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

<!-- single item exception request dialog -->
<ProtectionRequestDialog
  bind:open={requestDialogOpen}
  media={requestTarget ? toMediaLike(requestTarget) : null}
  mediaType={requestTarget?.media_type ?? MediaType.Movie}
  seasonId={requestTarget?.season_id ?? null}
  seasonNumber={requestTarget?.season_number ?? null}
  onSuccess={handleRequestSuccess}
/>

<div class="p-2.5 md:p-8 max-w-7xl mx-auto space-y-4">
  <div class="space-y-2">
    <h1 class="text-3xl font-bold text-foreground">Reclaim Candidates</h1>
    <p class="text-muted-foreground">
      Media flagged for deletion based on your configured rules.
      {#if canBulkSelect}
        Select multiple items for bulk actions.
      {/if}
    </p>
  </div>

  <!-- filters -->
  <div class="flex flex-col sm:flex-row gap-2">
    <div class="relative flex-1">
      <Search
        class="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted-foreground"
      />
      <Input
        type="text"
        placeholder="Search by title or reason"
        value={searchQuery}
        oninput={handleSearch}
        class="pl-10 bg-card"
      />
    </div>

    <div class="flex flex-1 flex-row gap-2">
      <Select.Root type="single" bind:value={sortBy}>
        <Select.Trigger class="flex-10 bg-card text-card-foreground">
          {sortByOptions.find((o) => o.value === sortBy)?.label}
        </Select.Trigger>
        <Select.Content class="bg-card">
          {#each sortByOptions as opt}
            <Select.Item
              value={opt.value}
              label={opt.label}
              class="text-card-foreground"
            >
              {opt.label}
            </Select.Item>
          {/each}
        </Select.Content>
      </Select.Root>

      <Select.Root type="single" bind:value={sortOrder}>
        <Select.Trigger class="flex-10 bg-card text-card-foreground">
          {sortOrder === "asc" ? "Ascending" : "Descending"}
        </Select.Trigger>
        <Select.Content class="bg-card">
          <Select.Item
            value="asc"
            label="Ascending"
            class="text-card-foreground">Ascending</Select.Item
          >
          <Select.Item
            value="desc"
            label="Descending"
            class="text-card-foreground">Descending</Select.Item
          >
        </Select.Content>
      </Select.Root>

      <Select.Root type="single" bind:value={mediaTypeFilter}>
        <Select.Trigger class="flex-10 bg-card text-card-foreground">
          {mediaTypeFilter === "all"
            ? "All Media"
            : mediaTypeFilter === MediaType.Movie
              ? "Movies"
              : "Series"}
        </Select.Trigger>
        <Select.Content class="bg-card">
          <Select.Item
            value="all"
            label="All Media"
            class="text-card-foreground">All Media</Select.Item
          >
          <Select.Item
            value={MediaType.Movie}
            label="Movies"
            class="text-card-foreground">Movies</Select.Item
          >
          <Select.Item
            value={MediaType.Series}
            label="Series"
            class="text-card-foreground">Series</Select.Item
          >
        </Select.Content>
      </Select.Root>
    </div>
  </div>

  <!-- bulk action bar -->
  {#if canBulkSelect && selectedIds.size > 0}
    <div
      class="flex items-center justify-between gap-4 px-4 py-3 bg-primary/10 border border-primary/30
        rounded-lg"
    >
      <span class="text-sm text-foreground font-medium">
        {selectedIds.size} item{selectedIds.size !== 1 ? "s" : ""} selected
      </span>
      <div class="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          class="cursor-pointer bg-destructive/80 hover:bg-destructive/60"
          onclick={() => (selectedIds = new Set())}
        >
          Clear
        </Button>
        {#if isAdmin}
          <Button
            size="sm"
            class="cursor-pointer"
            onclick={() => (bulkDialogOpen = true)}
          >
            <ShieldBan class="size-4" />
            Protect {selectedIds.size}
          </Button>
        {/if}
        {#if canDelete}
          <Button
            size="sm"
            class="cursor-pointer bg-destructive/80 hover:bg-destructive/60"
            onclick={() => (bulkDeleteDialogOpen = true)}
          >
            <Trash class="size-4" />
            Delete {selectedIds.size}
          </Button>
        {/if}
      </div>
    </div>
  {/if}

  <!-- error box -->
  <ErrorBox {error} />

  <div class="bg-card rounded-lg border border-border overflow-x-auto">
    {#if loading}
      <div class="p-8 text-center text-muted-foreground">
        <div
          class="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-primary
            border-r-transparent"
        ></div>
        <p class="mt-4">Loading candidates...</p>
      </div>
    {:else if entries.length === 0}
      <div class="p-8 text-center text-muted-foreground">
        No reclaim candidates found.
      </div>
    {:else}
      <table class="w-full">
        <thead class="bg-muted/50">
          <tr>
            {#if canBulkSelect}
              <th class="px-4 py-3 w-10">
                <input
                  type="checkbox"
                  checked={allPageSelected}
                  onchange={toggleSelectAll}
                  class="cursor-pointer accent-primary"
                />
              </th>
            {/if}
            <th
              class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
              >Media</th
            >
            <th
              class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
              >Reason</th
            >
            <th
              class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
              >Space</th
            >
            <th
              class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
              >Flagged</th
            >
            <th
              class="px-6 py-3 text-right text-xs font-medium text-muted-foreground uppercase tracking-wider"
              >Actions</th
            >
          </tr>
        </thead>
        <tbody class="divide-y divide-border">
          {#each displayRows() as row (row.kind === "flat" ? `flat-${row.entry.id}` : `group-${row.media_id}`)}
            {#if row.kind === "flat"}
              <!-- flat row (movie or whole series candidate) -->
              {@const entry = row.entry}
              <tr
                class="hover:bg-muted/30 transition-colors {selectedIds.has(
                  entry.id,
                )
                  ? 'bg-primary/5'
                  : ''}"
              >
                {#if canBulkSelect}
                  <td class="px-4 py-4 w-10">
                    <input
                      type="checkbox"
                      checked={selectedIds.has(entry.id)}
                      onchange={() => toggleSelect(entry.id)}
                      class="cursor-pointer accent-primary"
                    />
                  </td>
                {/if}
                <td class="px-6 py-4">
                  <div class="flex gap-4 items-center">
                    <div class="flex flex-col items-center w-max gap-1">
                      <PosterThumb
                        mediaType={entry.media_type}
                        posterUrl={entry.poster_url}
                      />
                      <MediaTypeBadge mediaType={entry.media_type} />
                    </div>
                    <div class="text-sm font-medium text-foreground">
                      {entry.media_title}
                      {#if entry.media_year}
                        <span class="text-muted-foreground"
                          >({entry.media_year})</span
                        >
                      {/if}
                    </div>
                  </div>
                </td>
                <td class="px-6 py-4 text-sm text-muted-foreground max-w-xs">
                  <span class="line-clamp-3">{entry.reason}</span>
                </td>
                <td class="px-6 py-4 text-sm text-foreground whitespace-nowrap">
                  {spaceLabel(entry.estimated_space_gb)}
                </td>
                <td
                  class="px-6 py-4 text-sm text-muted-foreground whitespace-nowrap"
                >
                  {formatDate(entry.created_at)}
                </td>
                <td class="px-6 py-4 text-right whitespace-nowrap">
                  <div class="flex gap-2 justify-end items-center">
                    {#if entry.has_pending_request}
                      <span class="text-xs text-blue-400">Pending request</span>
                    {:else}
                      <Button
                        size="icon"
                        class="cursor-pointer rounded-full"
                        onclick={() => openSingleRequest(entry)}
                      >
                        <ShieldBan class="size-4" />
                      </Button>
                    {/if}
                    {#if canDelete}
                      <Button
                        size="icon"
                        class="cursor-pointer rounded-full bg-destructive/80 hover:bg-destructive/60"
                        onclick={() => openSingleDelete(entry)}
                      >
                        <Trash class="size-4" />
                      </Button>
                    {/if}
                  </div>
                </td>
              </tr>
            {:else}
              <!-- group header row (series with season candidates) -->
              {@const expanded = expandedGroups.has(row.media_id)}
              {@const allSel = isGroupAllSelected(row)}
              {@const partSel = isGroupPartialSelected(row)}
              <tr
                class="hover:bg-muted/30 transition-colors cursor-pointer {allSel
                  ? 'bg-primary/5'
                  : ''}"
                onclick={() => toggleExpand(row.media_id)}
              >
                {#if canBulkSelect}
                  <td
                    class="px-4 py-4 w-10"
                    onclick={(e) => e.stopPropagation()}
                  >
                    <input
                      type="checkbox"
                      checked={allSel}
                      indeterminate={partSel}
                      onchange={() => toggleGroupSelect(row)}
                      class="cursor-pointer accent-primary"
                    />
                  </td>
                {/if}
                <td class="px-6 py-4">
                  <div class="flex gap-4 items-center">
                    <div class="flex flex-col items-center w-max gap-1">
                      <PosterThumb
                        mediaType={MediaType.Series}
                        posterUrl={row.poster_url}
                      />
                      <MediaTypeBadge mediaType={MediaType.Series} />
                    </div>
                    <div class="text-sm font-medium text-foreground">
                      {row.media_title}
                      {#if row.media_year}
                        <span class="text-muted-foreground"
                          >({row.media_year})</span
                        >
                      {/if}
                      <div class="mt-0.5">
                        <span class="text-xs text-amber-400 font-normal">
                          {row.seasons.length} season{row.seasons.length !== 1
                            ? "s"
                            : ""} flagged
                        </span>
                      </div>
                    </div>
                  </div>
                </td>
                <td class="px-6 py-4 text-sm text-muted-foreground">
                  {#if row.seriesEntry}
                    <span class="line-clamp-2">{row.seriesEntry.reason}</span>
                  {:else}
                    <span class="italic opacity-60"
                      >Season-level only ? click to expand</span
                    >
                  {/if}
                </td>
                <td class="px-6 py-4 text-sm text-foreground whitespace-nowrap">
                  {groupTotalGb(row).toFixed(2)} GB
                </td>
                <td
                  class="px-6 py-4 text-sm text-muted-foreground whitespace-nowrap"
                >
                  {formatDate(row.seasons[0].created_at)}
                </td>
                <td class="px-6 py-4 text-right whitespace-nowrap">
                  <div class="flex gap-2 justify-end items-center">
                    <ChevronRight
                      class="size-4 text-muted-foreground transition-transform duration-200 {expanded
                        ? 'rotate-90'
                        : ''}"
                    />
                  </div>
                </td>
              </tr>

              <!-- expanded season sub-rows -->
              {#if expanded}
                {#each row.seasons as season (season.id)}
                  <tr
                    class="bg-muted/20 border-l-2 border-l-amber-500/40 hover:bg-muted/40 transition-colors
                      {selectedIds.has(season.id) ? 'bg-primary/5' : ''}"
                  >
                    {#if canBulkSelect}
                      <td class="px-4 py-3 w-10 pl-8">
                        <input
                          type="checkbox"
                          checked={selectedIds.has(season.id)}
                          onchange={() => toggleSelect(season.id)}
                          class="cursor-pointer accent-primary"
                        />
                      </td>
                    {/if}
                    <td class="px-6 py-3 pl-14">
                      <span class="text-sm font-medium text-foreground">
                        Season {season.season_number}
                      </span>
                    </td>
                    <td
                      class="px-6 py-3 text-sm text-muted-foreground max-w-xs"
                    >
                      <span class="line-clamp-2">{season.reason}</span>
                    </td>
                    <td
                      class="px-6 py-3 text-sm text-foreground whitespace-nowrap"
                    >
                      {spaceLabel(season.estimated_space_gb)}
                    </td>
                    <td
                      class="px-6 py-3 text-sm text-muted-foreground whitespace-nowrap"
                    >
                      {formatDate(season.created_at)}
                    </td>
                    <td class="px-6 py-3 text-right whitespace-nowrap">
                      <div class="flex gap-2 justify-end items-center">
                        {#if season.has_pending_request}
                          <span class="text-xs text-blue-400"
                            >Pending request</span
                          >
                        {:else}
                          <Button
                            size="icon"
                            class="cursor-pointer rounded-full size-7"
                            onclick={() => openSingleRequest(season)}
                          >
                            <ShieldBan class="size-3.5" />
                          </Button>
                        {/if}
                        {#if canDelete}
                          <Button
                            size="icon"
                            class="cursor-pointer rounded-full size-7 bg-destructive/80 hover:bg-destructive/60"
                            onclick={() => openSingleDelete(season)}
                          >
                            <Trash class="size-3.5" />
                          </Button>
                        {/if}
                      </div>
                    </td>
                  </tr>
                {/each}
              {/if}
            {/if}
          {/each}
        </tbody>
      </table>
    {/if}
  </div>

  {#if !loading && entries.length !== 0 && data && data.total_pages > 1}
    <div
      class="flex flex-wrap justify-center gap-2 md:flex-nowrap md:justify-between items-center"
    >
      <p class="text-sm text-muted-foreground">
        Showing {(data.page - 1) * data.per_page + 1} to {Math.min(
          data.page * data.per_page,
          data.total,
        )} of {data.total} candidates
      </p>
      <CompactPagination
        currentPage={data.page}
        totalPages={data.total_pages}
        maxVisiblePages={3}
        onPageChange={loadCandidates}
      />
    </div>
  {/if}
</div>
