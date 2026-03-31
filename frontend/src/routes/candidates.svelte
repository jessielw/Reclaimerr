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
  import ProtectionRequestDialog from "$lib/components/media/protection-request-dialog.svelte";
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import ShieldBan from "@lucide/svelte/icons/shield-ban";

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

  // selection (admins only)
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

  const isAdmin = $derived(
    $auth.user?.role === UserRole.Admin ||
      ($auth.user?.permissions ?? []).includes(Permission.AutoApprove),
  );

  const canDelete = $derived(
    $auth.user?.role === UserRole.Admin ||
      ($auth.user?.permissions ?? []).includes(Permission.ManageReclaim),
  );

  const canBulkSelect = $derived(isAdmin || canDelete);

  const allPageSelected = $derived(
    entries.length > 0 && entries.every((e) => selectedIds.has(e.id)),
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

  const toggleSelectAll = () => {
    if (allPageSelected) {
      selectedIds = new Set();
    } else {
      selectedIds = new Set(entries.map((e) => e.id));
    }
  };

  const openSingleRequest = (entry: ReclaimCandidateEntry) => {
    requestTarget = entry;
    requestDialogOpen = true;
  };

  const openSingleDelete = (entry: ReclaimCandidateEntry) => {
    deleteTarget = entry;
    deleteDialogOpen = true;
  };

  const submitSingleDelete = async () => {
    if (!deleteTarget) return;
    deleteSubmitting = true;
    try {
      const resp = await post_api<DeleteResponse>(
        "/api/media/candidates/delete",
        {
          candidate_ids: [deleteTarget.id],
        },
      );
      if (resp.deleted > 0) {
        toast.success(`Deleted ${deleteTarget.media_title}.`);
        if (!data) {
          await loadCandidates(currentPage);
          return;
        }
        const remaining = data.items.filter((i) => i.id !== deleteTarget!.id);
        const newTotal = Math.max(0, data.total - 1);
        if (remaining.length === 0 && currentPage > 1) {
          await loadCandidates(currentPage - 1);
        } else {
          data = {
            ...data,
            items: remaining,
            total: newTotal,
            total_pages: newTotal > 0 ? Math.ceil(newTotal / data.per_page) : 0,
          };
        }
      } else {
        toast.error(`Failed to delete ${deleteTarget.media_title}.`);
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
        {
          candidate_ids: selectedEntries.map((e) => e.id),
        },
      );
      if (resp.deleted > 0)
        toast.success(
          `Deleted ${resp.deleted} item${resp.deleted !== 1 ? "s" : ""}.`,
        );
      if (resp.failed > 0)
        toast.error(
          `${resp.failed} item${resp.failed !== 1 ? "s" : ""} could not be deleted.`,
        );

      if (data && resp.deleted > 0) {
        // only remove the first resp.deleted items from selected (we don't know which failed)
        // simplest: reload to get accurate state
        await loadCandidates(currentPage);
      }
    } catch (e: any) {
      toast.error(e.message ?? "Failed to delete candidates.");
    } finally {
      bulkDeleteSubmitting = false;
      bulkDeleteDialogOpen = false;
      selectedIds = new Set();
    }
  };

  // after a single request succeeds, mark the row as pending
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

  // bulk request: fire all in parallel, collect results
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

    // update local state for succeeded items
    const succeededMediaIds = new Set(
      results
        .filter(
          (r): r is PromiseFulfilledResult<ProtectionRequest> =>
            r.status === "fulfilled",
        )
        .map((r) => r.value.media_id),
    );

    if (data && succeededMediaIds.size > 0) {
      data = {
        ...data,
        items: data.items.map((item) =>
          succeededMediaIds.has(item.media_id)
            ? { ...item, has_pending_request: true }
            : item,
        ),
      };
    }

    selectedIds = new Set();
    bulkDialogOpen = false;
    bulkSubmitting = false;
  };

  // convert a candidate entry into the shape ProtectionRequestDialog expects
  const toMediaLike = (entry: ReclaimCandidateEntry) => ({
    id: entry.media_id,
    title: entry.media_title,
    year: entry.media_year,
    poster_url: entry.poster_url,
    status: { is_candidate: true },
  });

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
          Permanently delete <strong>{deleteTarget.media_title}</strong> from from
          all configured services and remove the files from disk. This cannot be undone.
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
          {#each entries as entry (entry.id)}
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
                {entry.estimated_space_gb != null
                  ? `${entry.estimated_space_gb.toFixed(2)} GB`
                  : "—"}
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
