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
    type ReclaimHistoryEntry,
    type ProtectionRequest,
    type PaginatedResponse,
  } from "$lib/types/shared";
  import { formatDate } from "$lib/utils/date";
  import { formatFileSize } from "$lib/utils/formatters";
  import {
    createPerPageState,
    createFilterState,
    PER_PAGE_OPTIONS,
  } from "$lib/utils/pagination";
  import { toast } from "svelte-sonner";
  import Search from "@lucide/svelte/icons/search";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import FolderOutput from "@lucide/svelte/icons/folder-output";
  import History from "@lucide/svelte/icons/history";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
  import ClapperBoard from "@lucide/svelte/icons/clapperboard";
  import Tv from "@lucide/svelte/icons/tv";
  import ProtectionRequestDialog from "$lib/components/media/protection-request-dialog.svelte";
  import Shield from "@lucide/svelte/icons/shield";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import MovieCandidatesView from "$lib/components/candidates/movie-candidates-view.svelte";
  import SeriesCandidatesView from "$lib/components/candidates/series-candidates-view.svelte";

  interface DeleteResponse {
    deleted: number;
    failed: number;
  }

  interface MoveResponse {
    moved: number;
    failed: number;
  }

  // display row (either a flat entry, a series season group, or a movie version group)
  type FlatRow = { kind: "flat"; entry: ReclaimCandidateEntry };
  type SeriesGroupRow = {
    kind: "group";
    group_type: "series_seasons";
    seriesEntry: ReclaimCandidateEntry | null;
    seasons: ReclaimCandidateEntry[];
    versions: ReclaimCandidateEntry[];
    media_id: number;
    media_title: string;
    media_year: number | null;
    poster_url: string | null;
  };
  type MovieGroupRow = {
    kind: "group";
    group_type: "movie_versions";
    seriesEntry: ReclaimCandidateEntry | null;
    seasons: ReclaimCandidateEntry[];
    versions: ReclaimCandidateEntry[];
    media_id: number;
    media_title: string;
    media_year: number | null;
    poster_url: string | null;
  };
  type GroupRow = SeriesGroupRow | MovieGroupRow;
  type DisplayRow = FlatRow | GroupRow;

  // state
  let data = $state<PaginatedResponse<ReclaimCandidateEntry> | null>(null);
  let loading = $state(true);
  let error = $state("");
  let searchQuery = $state("");
  const _tabStore = createFilterState("candidates_active_tab", MediaType.Movie);
  const _sortByStore = createFilterState("candidates_sort_by", "created_at");
  const _sortOrderStore = createFilterState("candidates_sort_order", "desc");
  let activeTab = $state(
    _tabStore.getInitial() === MediaType.Series
      ? MediaType.Series
      : MediaType.Movie,
  );

  // top level view: candidates vs history
  const _viewStore = createFilterState("candidates_active_view", "candidates");
  let activeView = $state<"candidates" | "history">(
    _viewStore.getInitial() === "history" ? "history" : "candidates",
  );

  // history tab state
  let historyData = $state<PaginatedResponse<ReclaimHistoryEntry> | null>(null);
  let historyLoading = $state(false);
  let historyError = $state("");
  let historyPage = $state(1);
  let historySearch = $state("");
  let historySearchTimer: ReturnType<typeof setTimeout> | null = null;
  let historyAbortController: AbortController | null = null;
  const _historyPerPageStore = createPerPageState("history_per_page");
  let historyPerPage = $state(_historyPerPageStore.getInitial());
  const _historyMediaTypeStore = createFilterState("history_media_type", "all");
  let historyMediaType = $state(_historyMediaTypeStore.getInitial());
  const _historySortOrderStore = createFilterState(
    "history_sort_order",
    "desc",
  );
  let historySortOrder = $state<"asc" | "desc">(
    _historySortOrderStore.getInitial() === "asc" ? "asc" : "desc",
  );
  let sortBy = $state(_sortByStore.getInitial());
  let sortOrder = $state(_sortOrderStore.getInitial());
  let currentPage = $state(1);

  const _perPageStore = createPerPageState("candidates_per_page");
  let perPage = $state(_perPageStore.getInitial());

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

  // single item move
  let moveDialogOpen = $state(false);
  let moveTarget = $state<ReclaimCandidateEntry | null>(null);
  let moveSubmitting = $state(false);

  // bulk move
  let bulkMoveDialogOpen = $state(false);
  let bulkMoveSubmitting = $state(false);

  // whether move is enabled (loaded from general settings)
  let moveEnabled = $state(false);

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
    { value: "estimated_space_bytes", label: "Size" },
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
  const displayRows = $derived((): DisplayRow[] => {
    const seasonGroups = new Map<number, ReclaimCandidateEntry[]>();
    const versionGroups = new Map<number, ReclaimCandidateEntry[]>();
    const seriesFlat = new Map<number, ReclaimCandidateEntry>();
    const flatRows: DisplayRow[] = [];

    for (const e of entries) {
      if (
        e.media_type === MediaType.Movie &&
        e.movie_version_id != null &&
        e.season_id == null
      ) {
        const g = versionGroups.get(e.media_id) ?? [];
        g.push(e);
        versionGroups.set(e.media_id, g);
      } else if (e.season_id != null) {
        const g = seasonGroups.get(e.media_id) ?? [];
        g.push(e);
        seasonGroups.set(e.media_id, g);
      } else if (e.media_type === MediaType.Series) {
        seriesFlat.set(e.media_id, e);
      } else {
        flatRows.push({ kind: "flat", entry: e });
      }
    }

    for (const [mid, fe] of seriesFlat) {
      if (seasonGroups.has(mid)) {
      } else {
        flatRows.push({ kind: "flat", entry: fe });
      }
    }

    const result: DisplayRow[] = [...flatRows];

    for (const [mid, seasons] of seasonGroups) {
      const sorted = [...seasons].sort((a, b) => {
        const sA = a.season_number ?? 0;
        const sB = b.season_number ?? 0;
        if (sA !== sB) return sA - sB;
        return (a.episode_number ?? 0) - (b.episode_number ?? 0);
      });
      const first = sorted[0];
      result.push({
        kind: "group",
        group_type: "series_seasons",
        seriesEntry: seriesFlat.get(mid) ?? null,
        seasons: sorted,
        versions: [],
        media_id: mid,
        media_title: first.series_title ?? first.media_title,
        media_year: first.media_year,
        poster_url: first.poster_url,
      });
    }

    for (const [mid, versions] of versionGroups) {
      const sorted = [...versions].sort((a, b) => {
        const sa = a.version_size ?? 0;
        const sb = b.version_size ?? 0;
        return sb - sa;
      });
      const first = sorted[0];
      result.push({
        kind: "group",
        group_type: "movie_versions",
        seriesEntry: null,
        seasons: [],
        versions: sorted,
        media_id: mid,
        media_title: first.media_title,
        media_year: first.media_year,
        poster_url: first.poster_url,
      });
    }

    return result;
  });

  const movieRows = $derived(
    displayRows().filter(
      (row): row is FlatRow | MovieGroupRow =>
        row.kind === "flat" ||
        (row.kind === "group" && row.group_type === "movie_versions"),
    ),
  );

  const seriesRows = $derived(
    displayRows().filter(
      (row): row is FlatRow | SeriesGroupRow =>
        row.kind === "flat" ||
        (row.kind === "group" && row.group_type === "series_seasons"),
    ),
  );

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
        if (row.group_type === "series_seasons") {
          if (row.seriesEntry) ids.push(row.seriesEntry.id);
          for (const s of row.seasons) ids.push(s.id);
        } else {
          for (const v of row.versions) ids.push(v.id);
        }
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

  const selectedTotalBytes = $derived(
    selectedEntries.reduce((acc, e) => acc + (e.estimated_space_bytes ?? 0), 0),
  );

  $effect(() => _tabStore.save(activeTab));
  $effect(() => _sortByStore.save(sortBy));
  $effect(() => _sortOrderStore.save(sortOrder));
  $effect(() => _viewStore.save(activeView));
  $effect(() => _historyMediaTypeStore.save(historyMediaType));
  $effect(() => _historySortOrderStore.save(historySortOrder));

  $effect(() => {
    activeTab;
    sortBy;
    sortOrder;
    perPage;
    if (mounted) loadCandidates(1);
  });

  $effect(() => {
    historyPerPage;
    historyMediaType;
    historySortOrder;
    if (mounted) loadHistory(1);
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
        per_page: perPage.toString(),
        sort_by: sortBy,
        sort_order: sortOrder,
      });

      if (searchQuery.trim()) params.append("search", searchQuery.trim());
      params.append("media_type", activeTab);

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

  // load candidate history
  const loadHistory = async (page: number = historyPage) => {
    if (historyAbortController) historyAbortController.abort();
    historyAbortController = new AbortController();
    const signal = historyAbortController.signal;

    historyLoading = true;
    historyError = "";
    historyPage = page;

    try {
      const params = new URLSearchParams({
        page: page.toString(),
        per_page: historyPerPage.toString(),
      });
      if (historySearch.trim()) params.append("search", historySearch.trim());
      if (historyMediaType !== "all")
        params.append("media_type", historyMediaType);
      params.append("sort_order", historySortOrder);

      historyData = await get_api<PaginatedResponse<ReclaimHistoryEntry>>(
        `/api/media/reclaim-history?${params.toString()}`,
        signal,
      );
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      historyError = e.message ?? "Failed to load history.";
    } finally {
      if (!signal.aborted) historyLoading = false;
    }
  };

  // search candidate history
  const handleHistorySearch = (event: Event) => {
    historySearch = (event.target as HTMLInputElement).value;
    if (historySearchTimer) clearTimeout(historySearchTimer);
    historySearchTimer = setTimeout(() => loadHistory(1), 400);
  };

  // toggle selection of an individual candidate ID
  const toggleSelect = (id: number) => {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedIds = next;
  };

  // toggle all seasons in a group
  const toggleGroupSelect = (row: GroupRow) => {
    const groupIds =
      row.group_type === "series_seasons"
        ? [
            ...row.seasons.map((s) => s.id),
            ...(row.seriesEntry ? [row.seriesEntry.id] : []),
          ]
        : row.versions.map((v) => v.id);
    const allSelected = groupIds.every((id) => selectedIds.has(id));
    const next = new Set(selectedIds);
    if (allSelected) groupIds.forEach((id) => next.delete(id));
    else groupIds.forEach((id) => next.add(id));
    selectedIds = next;
  };

  // a group is fully selected if all season entries + the series entry (if exists) are selected
  const isGroupAllSelected = (row: GroupRow): boolean => {
    const groupIds =
      row.group_type === "series_seasons"
        ? [
            ...row.seasons.map((s) => s.id),
            ...(row.seriesEntry ? [row.seriesEntry.id] : []),
          ]
        : row.versions.map((v) => v.id);
    return groupIds.length > 0 && groupIds.every((id) => selectedIds.has(id));
  };

  // a group is partially selected if some (but not all) season entries or the series entry are selected
  const isGroupPartialSelected = (row: GroupRow): boolean => {
    const groupIds =
      row.group_type === "series_seasons"
        ? [
            ...row.seasons.map((s) => s.id),
            ...(row.seriesEntry ? [row.seriesEntry.id] : []),
          ]
        : row.versions.map((v) => v.id);
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

  const openSingleMove = (entry: ReclaimCandidateEntry) => {
    moveTarget = entry;
    moveDialogOpen = true;
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

  // After bulk move/request/delete, remove the affected candidates from the current list.
  // If all candidates on the current page are removed and it's not the first page, load the previous page.
  const applyBulkRemoval = async (idsToRemove: Set<number>) => {
    if (!data || idsToRemove.size === 0) return;
    const remaining = data.items.filter((i) => !idsToRemove.has(i.id));
    if (remaining.length === 0 && currentPage > 1) {
      await loadCandidates(currentPage - 1);
      return;
    }
    removeCandidateIds(idsToRemove);
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
        const episodePart =
          deleteTarget.episode_number != null
            ? `S${String(deleteTarget.season_number ?? 0).padStart(2, "0")}E${String(deleteTarget.episode_number).padStart(2, "0")}${deleteTarget.episode_name ? ` "${deleteTarget.episode_name}"` : ""}`
            : null;
        const label =
          episodePart != null
            ? `${episodePart} of ${deleteTarget.series_title ?? deleteTarget.media_title}`
            : deleteTarget.season_number != null
              ? `S${String(deleteTarget.season_number).padStart(2, "0")} of ` +
                `${deleteTarget.series_title ?? deleteTarget.media_title}`
              : deleteTarget.movie_version_id != null
                ? `${deleteTarget.media_title} (${deleteTarget.version_library_name ?? "version"})`
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
    const selectedCandidateIds = selectedEntries.map((e) => e.id);
    try {
      const resp = await post_api<DeleteResponse>(
        "/api/media/candidates/delete",
        { candidate_ids: selectedCandidateIds },
      );
      if (resp.deleted > 0)
        toast.success(
          `Deleted ${resp.deleted} item${resp.deleted !== 1 ? "s" : ""}.`,
        );
      if (resp.failed > 0)
        toast.error(
          `${resp.failed} item${resp.failed !== 1 ? "s" : ""} could not be deleted.`,
        );
      if (resp.deleted > 0) {
        const idsToRemove = new Set(
          selectedCandidateIds.slice(0, resp.deleted),
        );
        await applyBulkRemoval(idsToRemove);
      }
    } catch (e: any) {
      toast.error(e.message ?? "Failed to delete candidates.");
    } finally {
      bulkDeleteSubmitting = false;
      bulkDeleteDialogOpen = false;
      selectedIds = new Set();
    }
  };

  const submitSingleMove = async () => {
    if (!moveTarget) return;
    moveSubmitting = true;
    try {
      const resp = await post_api<MoveResponse>("/api/media/candidates/move", {
        candidate_ids: [moveTarget.id],
      });
      if (resp.moved > 0) {
        const label = moveTarget.media_title;
        toast.success(`Moved ${label}.`);
        const remaining = data?.items.filter((i) => i.id !== moveTarget!.id);
        if (!remaining || (remaining.length === 0 && currentPage > 1)) {
          await loadCandidates(currentPage - 1);
        } else {
          removeCandidateIds(new Set([moveTarget.id]));
        }
      } else {
        toast.error("Failed to move.");
      }
      moveDialogOpen = false;
      moveTarget = null;
    } catch (e: any) {
      toast.error(e.message ?? "Failed to move candidate.");
    } finally {
      moveSubmitting = false;
    }
  };

  const submitBulkMove = async () => {
    if (selectedEntries.length === 0) return;
    bulkMoveSubmitting = true;
    try {
      const resp = await post_api<MoveResponse>("/api/media/candidates/move", {
        candidate_ids: selectedEntries.map((e) => e.id),
      });
      if (resp.moved > 0)
        toast.success(
          `Moved ${resp.moved} item${resp.moved !== 1 ? "s" : ""}.`,
        );
      if (resp.failed > 0)
        toast.error(
          `${resp.failed} item${resp.failed !== 1 ? "s" : ""} could not be moved.`,
        );
      if (resp.moved > 0) await loadCandidates(currentPage);
    } catch (e: any) {
      toast.error(e.message ?? "Failed to move candidates.");
    } finally {
      bulkMoveSubmitting = false;
      bulkMoveDialogOpen = false;
      selectedIds = new Set();
    }
  };

  const handleRequestSuccess = (request: ProtectionRequest) => {
    if (!data) return;
    data = {
      ...data,
      items: data.items.map((item) =>
        item.media_id === request.media_id &&
        item.media_type === request.media_type &&
        (item.season_id ?? null) === (request.season_id ?? null) &&
        (item.movie_version_id ?? null) === (request.movie_version_id ?? null)
          ? { ...item, has_pending_request: true }
          : item,
      ),
    };
  };

  // Submit bulk protection request for all selected candidates. Duration is determined by the
  // bulkDuration state (either a preset duration, custom number of days, or permanent).
  const submitBulkRequest = async () => {
    if (selectedEntries.length === 0) return;
    bulkSubmitting = true;
    const selectedWithIds = selectedEntries.map((entry) => ({
      candidateId: entry.id,
      entry,
    }));

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
      selectedWithIds.map(({ entry }) =>
        post_api<ProtectionRequest>("/api/protection-requests", {
          media_type: entry.media_type,
          media_id: entry.media_id,
          movie_version_id: entry.movie_version_id ?? undefined,
          season_id: entry.season_id ?? undefined,
          reason: "Admin decision",
          duration_days: durationDays,
        }),
      ),
    );

    // All requests that were successfully submitted (regardless of auto-approval)
    // should be removed from the candidate list, since they now have pending requests
    // and can't be submitted again until those are resolved.
    const succeededIds = selectedWithIds
      .map((item, idx) => ({ item, result: results[idx] }))
      .filter(({ result }) => result.status === "fulfilled")
      .map(({ item }) => item.candidateId);
    const succeeded = succeededIds.length;
    const failed = results.length - succeeded;

    if (succeeded > 0)
      toast.success(
        `Submitted ${succeeded} protection request${succeeded !== 1 ? "s" : ""}`,
      );
    if (failed > 0)
      toast.error(`${failed} request${failed !== 1 ? "s" : ""} failed`);

    if (succeededIds.length > 0) {
      await applyBulkRemoval(new Set(succeededIds));
    }

    selectedIds = new Set();
    bulkDialogOpen = false;
    bulkSubmitting = false;
  };

  const toMediaLike = (entry: ReclaimCandidateEntry) => ({
    id: entry.media_id,
    movie_version_id: entry.movie_version_id,
    title: entry.series_title ?? entry.media_title,
    year: entry.media_year,
    poster_url: entry.poster_url,
    status: { is_candidate: true },
  });

  const groupTotalBytes = (row: GroupRow): number =>
    row.group_type === "series_seasons"
      ? row.seasons.reduce(
          (acc, s) => acc + (s.estimated_space_bytes ?? 0),
          0,
        ) + (row.seriesEntry?.estimated_space_bytes ?? 0)
      : row.versions.reduce(
          (acc, v) => acc + (v.estimated_space_bytes ?? 0),
          0,
        );

  const toggleMovieGroupSelect = (row: MovieGroupRow) => toggleGroupSelect(row);
  const toggleSeriesGroupSelect = (row: SeriesGroupRow) =>
    toggleGroupSelect(row);
  const isMovieGroupAllSelected = (row: MovieGroupRow) =>
    isGroupAllSelected(row);
  const isSeriesGroupAllSelected = (row: SeriesGroupRow) =>
    isGroupAllSelected(row);
  const isMovieGroupPartialSelected = (row: MovieGroupRow) =>
    isGroupPartialSelected(row);
  const isSeriesGroupPartialSelected = (row: SeriesGroupRow) =>
    isGroupPartialSelected(row);
  const movieGroupTotalBytes = (row: MovieGroupRow) => groupTotalBytes(row);
  const seriesGroupTotalBytes = (row: SeriesGroupRow) => groupTotalBytes(row);

  onMount(async () => {
    mounted = true;
    await loadCandidates();
    if (activeView === "history") await loadHistory();
    // load move enabled state from general settings
    try {
      const settings = await get_api<{ move_enabled?: boolean }>(
        "/api/settings/general",
      );
      moveEnabled = settings?.move_enabled ?? false;
    } catch {
      // non critical (move button simply won't appear)
    }
  });

  onDestroy(() => {
    if (searchTimer) clearTimeout(searchTimer);
    if (abortController) abortController.abort();
    if (historySearchTimer) clearTimeout(historySearchTimer);
    if (historyAbortController) historyAbortController.abort();
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
          {#if deleteTarget.episode_number != null}
            Permanently delete episode <strong
              >S{String(deleteTarget.season_number ?? 0).padStart(
                2,
                "0",
              )}E{String(deleteTarget.episode_number).padStart(
                2,
                "0",
              )}{#if deleteTarget.episode_name}&nbsp;&quot;{deleteTarget.episode_name}&quot;{/if}
              of {deleteTarget.series_title ?? deleteTarget.media_title}</strong
            > and remove the file from disk. Only this episode is affected — the series
            and season remain monitored. This cannot be undone.
          {:else if deleteTarget.season_number != null}
            Permanently delete <strong
              >Season {deleteTarget.season_number} of {deleteTarget.series_title ??
                deleteTarget.media_title}</strong
            > from all configured services and remove the files from disk. This cannot
            be undone.
          {:else if deleteTarget.movie_version_id != null}
            Permanently delete this version of <strong
              >{deleteTarget.media_title}</strong
            >
            from all configured services and remove the files from disk. This cannot
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

<!-- bulk move confirmation -->
<AlertDialog.Root bind:open={bulkMoveDialogOpen}>
  <AlertDialog.Content class="text-foreground">
    <AlertDialog.Header>
      <AlertDialog.Title
        >Move {selectedEntries.length} Item{selectedEntries.length !== 1
          ? "s"
          : ""}?</AlertDialog.Title
      >
      <AlertDialog.Description>
        This will move {selectedEntries.length} item{selectedEntries.length !==
        1
          ? "s"
          : ""} to the configured destination folder and remove them from the media
        server. Files will not be deleted from disk.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel disabled={bulkMoveSubmitting}
        >Cancel</AlertDialog.Cancel
      >
      <AlertDialog.Action
        onclick={submitBulkMove}
        disabled={bulkMoveSubmitting}
      >
        {bulkMoveSubmitting ? "Moving..." : `Move ${selectedEntries.length}`}
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<!-- single move confirmation -->
<AlertDialog.Root bind:open={moveDialogOpen}>
  <AlertDialog.Content class="text-foreground">
    <AlertDialog.Header>
      <AlertDialog.Title>Move to Destination?</AlertDialog.Title>
      <AlertDialog.Description>
        {#if moveTarget}
          Move <strong>{moveTarget.media_title}</strong> to the configured destination
          folder and remove it from the media server. The file will not be deleted
          from disk.
        {/if}
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel disabled={moveSubmitting}>Cancel</AlertDialog.Cancel>
      <AlertDialog.Action onclick={submitSingleMove} disabled={moveSubmitting}>
        {moveSubmitting ? "Moving..." : "Move"}
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

<div class="p-2.5 md:p-8">
  <div class="max-w-7xl mx-auto space-y-4">
    <div>
      <h1 class="text-3xl font-bold text-foreground">
        Reclaim Candidates {activeView === "candidates" ? "" : "(History)"}
      </h1>
      <p class="text-muted-foreground">
        Media flagged for deletion based on your configured rules.
        {#if canBulkSelect && activeView === "candidates"}
          Select multiple items for bulk actions.
        {/if}
      </p>
    </div>

    <!-- top-level view switcher: Candidates | History -->
    <div class="inline-flex rounded-md border border-border p-1 bg-card">
      <Button
        size="sm"
        class="cursor-pointer {activeView === 'candidates'
          ? 'bg-primary text-background dark:text-foreground'
          : 'text-foreground bg-transparent'}"
        onclick={() => {
          activeView = "candidates";
        }}
      >
        <TriangleAlert class="size-4 " />
        Candidates
      </Button>
      <Button
        size="sm"
        class="cursor-pointer {activeView === 'history'
          ? 'bg-primary text-background dark:text-foreground'
          : 'text-foreground bg-transparent'}"
        onclick={() => {
          activeView = "history";
          if (!historyData) loadHistory(1);
        }}
      >
        <History class="size-4 " />
        History
      </Button>
    </div>

    {#if activeView === "candidates"}
      <div class="inline-flex rounded-md border border-border p-1 bg-card">
        <Button
          size="sm"
          class="cursor-pointer {activeTab === MediaType.Movie
            ? 'bg-primary text-background dark:text-foreground'
            : 'text-foreground bg-transparent'}"
          onclick={() => {
            activeTab = MediaType.Movie;
            selectedIds = new Set();
            expandedGroups = new Set();
          }}
        >
          <ClapperBoard class="size-4 " />
          Movies
        </Button>
        <Button
          size="sm"
          class="cursor-pointer {activeTab === MediaType.Series
            ? 'bg-primary text-background dark:text-foreground'
            : 'text-foreground bg-transparent'}"
          onclick={() => {
            activeTab = MediaType.Series;
            selectedIds = new Set();
            expandedGroups = new Set();
          }}
        >
          <Tv class="size-4 " />
          Series
        </Button>
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

        <div class="flex flex-1 flex-col gap-2 sm:flex-row">
          <!-- row 1 on mobile: sort by + sort order -->
          <div class="flex flex-1 gap-2">
            <Select.Root type="single" bind:value={sortBy}>
              <Select.Trigger class="flex-1 bg-card text-card-foreground">
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
              <Select.Trigger class="flex-1 bg-card text-card-foreground">
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
          </div>

          <!-- row 2 on mobile: per page -->
          <div class="flex flex-1 gap-2">
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
            <div class="flex-1"></div>
          </div>
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
            <span class="text-muted-foreground font-normal">
              - {formatFileSize(selectedTotalBytes)}
            </span>
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
              <!-- protect -->
              <Tooltip.Root>
                <Tooltip.Trigger>
                  <Button
                    size="sm"
                    class="cursor-pointer"
                    onclick={() => (bulkDialogOpen = true)}
                  >
                    <Shield class="size-4" />
                    Protect {selectedIds.size}
                  </Button>
                </Tooltip.Trigger>
                <Tooltip.Content>
                  <p>Protect</p>
                </Tooltip.Content>
              </Tooltip.Root>
            {/if}

            <!-- move -->
            {#if moveEnabled}
              <Tooltip.Root>
                <Tooltip.Trigger>
                  <Button
                    size="sm"
                    class="cursor-pointer bg-amber-500/80 hover:bg-amber-500/60"
                    onclick={() => (bulkMoveDialogOpen = true)}
                  >
                    <FolderOutput class="size-4" />
                    Move {selectedIds.size}
                  </Button>
                </Tooltip.Trigger>
                <Tooltip.Content>
                  <p>Move to destination folder</p>
                </Tooltip.Content>
              </Tooltip.Root>
            {/if}

            <!-- delete -->
            {#if canDelete}
              <Tooltip.Root>
                <Tooltip.Trigger>
                  <Button
                    size="sm"
                    class="cursor-pointer bg-destructive/80 hover:bg-destructive/60"
                    onclick={() => (bulkDeleteDialogOpen = true)}
                  >
                    <Trash2 class="size-4" />
                    Delete {selectedIds.size}
                  </Button>
                </Tooltip.Trigger>
                <Tooltip.Content>
                  <p>Delete</p>
                </Tooltip.Content>
              </Tooltip.Root>
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
        {:else if activeTab === MediaType.Movie}
          <MovieCandidatesView
            rows={movieRows}
            {canBulkSelect}
            {canDelete}
            {selectedIds}
            {expandedGroups}
            {allPageSelected}
            {toggleSelect}
            {toggleSelectAll}
            toggleGroupSelect={toggleMovieGroupSelect}
            isGroupAllSelected={isMovieGroupAllSelected}
            isGroupPartialSelected={isMovieGroupPartialSelected}
            {toggleExpand}
            {openSingleRequest}
            {openSingleDelete}
            {openSingleMove}
            {moveEnabled}
            {formatDate}
            groupTotalBytes={movieGroupTotalBytes}
          />
        {:else}
          <SeriesCandidatesView
            rows={seriesRows}
            {canBulkSelect}
            {canDelete}
            {selectedIds}
            {expandedGroups}
            {allPageSelected}
            {toggleSelect}
            {toggleSelectAll}
            toggleGroupSelect={toggleSeriesGroupSelect}
            isGroupAllSelected={isSeriesGroupAllSelected}
            isGroupPartialSelected={isSeriesGroupPartialSelected}
            {toggleExpand}
            {openSingleRequest}
            {openSingleDelete}
            {openSingleMove}
            {moveEnabled}
            {formatDate}
            groupTotalBytes={seriesGroupTotalBytes}
          />
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
    {:else}
      <!-- history view -->
      <div class="flex flex-col sm:flex-row gap-2">
        <div class="relative flex-1">
          <Search
            class="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted-foreground"
          />
          <Input
            type="text"
            placeholder="Search by title"
            value={historySearch}
            oninput={handleHistorySearch}
            class="pl-10 bg-card"
          />
        </div>
        <div class="flex gap-2">
          <Select.Root
            type="single"
            value={historyMediaType}
            onValueChange={(v) => {
              historyMediaType = v;
            }}
          >
            <Select.Trigger class="w-36 bg-card text-card-foreground">
              {historyMediaType === "all"
                ? "All Types"
                : historyMediaType === MediaType.Movie
                  ? "Movies"
                  : "Series"}
            </Select.Trigger>
            <Select.Content class="bg-card">
              <Select.Item
                value="all"
                label="All Types"
                class="text-card-foreground">All Types</Select.Item
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

          <Select.Root
            type="single"
            value={historySortOrder}
            onValueChange={(v) => {
              historySortOrder = v as "asc" | "desc";
            }}
          >
            <Select.Trigger class="w-32 bg-card text-card-foreground">
              {historySortOrder === "asc" ? "Oldest first" : "Newest first"}
            </Select.Trigger>
            <Select.Content class="bg-card">
              <Select.Item
                value="desc"
                label="Newest first"
                class="text-card-foreground">Newest first</Select.Item
              >
              <Select.Item
                value="asc"
                label="Oldest first"
                class="text-card-foreground">Oldest first</Select.Item
              >
            </Select.Content>
          </Select.Root>

          <Select.Root
            type="single"
            value={historyPerPage.toString()}
            onValueChange={(v) => {
              const n = parseInt(v, 10);
              if (!isNaN(n)) {
                historyPerPage = n;
                _historyPerPageStore.save(n);
              }
            }}
          >
            <Select.Trigger class="w-28 bg-card text-card-foreground">
              {historyPerPage} / page
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

      <ErrorBox error={historyError} />

      <div class="bg-card rounded-lg border border-border overflow-x-auto">
        {#if historyLoading}
          <div class="p-8 text-center text-muted-foreground">
            <div
              class="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-primary
            border-r-transparent"
            ></div>
            <p class="mt-4">Loading history...</p>
          </div>
        {:else if !historyData || historyData.items.length === 0}
          <div class="p-8 text-center text-muted-foreground">
            No reclaim history found.
          </div>
        {:else}
          <table class="w-full text-sm">
            <thead>
              <tr
                class="border-b border-border text-muted-foreground text-left"
              >
                <th class="px-4 py-3 font-medium">Title</th>
                <th class="px-4 py-3 font-medium">Type</th>
                <th class="px-4 py-3 font-medium">Action</th>
                <th class="px-4 py-3 font-medium">Size</th>
                <th class="px-4 py-3 font-medium">Deleted By</th>
                <th class="px-4 py-3 font-medium">Date</th>
              </tr>
            </thead>
            <tbody>
              {#each historyData.items as entry (entry.id)}
                <tr
                  class="border-b border-border last:border-0 hover:bg-muted/30"
                >
                  <td class="px-4 py-3 text-foreground">
                    {entry.name ?? "Unknown"}
                  </td>
                  <td class="px-4 py-3">
                    <span
                      class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium
                    {entry.media_type === MediaType.Movie
                        ? 'bg-blue-500/15 text-blue-400'
                        : 'bg-purple-500/15 text-purple-400'}"
                    >
                      {entry.media_type === MediaType.Movie
                        ? "Movie"
                        : "Series"}
                    </span>
                  </td>
                  <td class="px-4 py-3">
                    <span
                      class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium
                    {entry.action === 'moved'
                        ? 'bg-amber-500/15 text-amber-400'
                        : 'bg-red-500/15 text-red-400'}"
                    >
                      {entry.action === "moved" ? "Moved" : "Deleted"}
                    </span>
                  </td>
                  <td class="px-4 py-3 text-muted-foreground">
                    {entry.size != null ? formatFileSize(entry.size) : "-"}
                  </td>
                  <td class="px-4 py-3 text-muted-foreground">
                    {entry.approved_by}
                  </td>
                  <td class="px-4 py-3 text-muted-foreground">
                    {formatDate(entry.created_at)}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      </div>

      {#if !historyLoading && historyData && historyData.total_pages > 1}
        <div
          class="flex flex-wrap justify-center gap-2 md:flex-nowrap md:justify-between items-center"
        >
          <p class="text-sm text-muted-foreground">
            Showing {(historyData.page - 1) * historyData.per_page + 1} to {Math.min(
              historyData.page * historyData.per_page,
              historyData.total,
            )} of {historyData.total} records
          </p>
          <CompactPagination
            currentPage={historyData.page}
            totalPages={historyData.total_pages}
            maxVisiblePages={3}
            onPageChange={loadHistory}
          />
        </div>
      {/if}
    {/if}
  </div>
</div>
