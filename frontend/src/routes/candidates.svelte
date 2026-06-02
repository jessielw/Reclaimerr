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
    BackgroundJobStatus,
    MediaType,
    UserRole,
    Permission,
    type BackgroundJobRecord,
    type EpisodeWithStatus,
    type ReclaimCandidateEntry,
    type SeasonWithStatus,
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
  import { uiIndicators } from "$lib/stores/ui-indicators";
  import Search from "@lucide/svelte/icons/search";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import FolderOutput from "@lucide/svelte/icons/folder-output";
  import ProtectionRequestDialog from "$lib/components/media/protection-request-dialog.svelte";
  import Shield from "@lucide/svelte/icons/shield";
  import Eraser from "@lucide/svelte/icons/eraser";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import MixedCandidatesView from "$lib/components/candidates/mixed-candidates-view.svelte";
  import MovieCandidatesView from "$lib/components/candidates/movie-candidates-view.svelte";
  import SeriesCandidatesView from "$lib/components/candidates/series-candidates-view.svelte";
  import type {
    DisplayRow,
    FlatRow,
    GroupRow,
    MovieGroupRow,
    SeriesGroupRow,
  } from "$lib/components/candidates/view-types";

  interface CandidateOperationQueuedResponse {
    job_id: number | null;
    status: string;
    message: string;
  }

  interface CandidateFileOpResult {
    operation?: "delete" | "move";
    processed?: number;
    succeeded?: number;
    failed?: number;
  }

  // state
  let data = $state<PaginatedResponse<ReclaimCandidateEntry> | null>(null);
  let loading = $state(true);
  let error = $state("");
  let searchQuery = $state("");
  const _mediaFilterStore = createFilterState("candidates_media_filter", "all");
  const _sortByStore = createFilterState("candidates_sort_by", "created_at");
  const _sortOrderStore = createFilterState("candidates_sort_order", "desc");
  let mediaFilter = $state<"all" | MediaType.Movie | MediaType.Series>(
    _mediaFilterStore.getInitial() === MediaType.Movie
      ? MediaType.Movie
      : _mediaFilterStore.getInitial() === MediaType.Series
        ? MediaType.Series
        : "all",
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
    const seriesWithScopedEntries = new Set<number>();
    const seasonGroups = new Map<number, ReclaimCandidateEntry[]>();
    const versionGroups = new Map<number, ReclaimCandidateEntry[]>();
    const seriesFlat = new Map<number, ReclaimCandidateEntry>();
    const flatRows = new Map<number, ReclaimCandidateEntry>();
    const orderedKeys: string[] = [];
    const seenKeys = new Set<string>();

    const pushOrderedKey = (key: string) => {
      if (seenKeys.has(key)) return;
      seenKeys.add(key);
      orderedKeys.push(key);
    };

    for (const e of entries) {
      if (e.media_type === MediaType.Series && e.season_id != null) {
        seriesWithScopedEntries.add(e.media_id);
      }
    }

    for (const e of entries) {
      if (
        e.media_type === MediaType.Movie &&
        e.movie_version_id != null &&
        e.season_id == null
      ) {
        const g = versionGroups.get(e.media_id) ?? [];
        g.push(e);
        versionGroups.set(e.media_id, g);
        pushOrderedKey(`movie-group:${e.media_id}`);
      } else if (e.season_id != null) {
        const g = seasonGroups.get(e.media_id) ?? [];
        g.push(e);
        seasonGroups.set(e.media_id, g);
        pushOrderedKey(`series-group:${e.media_id}`);
      } else if (
        e.media_type === MediaType.Series &&
        seriesWithScopedEntries.has(e.media_id)
      ) {
        seriesFlat.set(e.media_id, e);
        pushOrderedKey(`series-group:${e.media_id}`);
      } else if (e.media_type === MediaType.Series) {
        flatRows.set(e.id, e);
        pushOrderedKey(`flat:${e.id}`);
      } else {
        flatRows.set(e.id, e);
        pushOrderedKey(`flat:${e.id}`);
      }
    }

    const result: DisplayRow[] = [];
    for (const key of orderedKeys) {
      if (key.startsWith("flat:")) {
        const entry = flatRows.get(Number(key.slice(5)));
        if (entry) result.push({ kind: "flat", entry });
        continue;
      }

      if (key.startsWith("series-group:")) {
        const mid = Number(key.slice("series-group:".length));
        const seasons = seasonGroups.get(mid) ?? [];
        if (seasons.length === 0) continue;
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
        continue;
      }

      if (key.startsWith("movie-group:")) {
        const mid = Number(key.slice("movie-group:".length));
        const versions = versionGroups.get(mid) ?? [];
        if (versions.length === 0) continue;
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
  const selectableOnPageCount = $derived(allSelectableIds().length);
  const selectedOnPageCount = $derived(
    allSelectableIds().filter((id) => selectedIds.has(id)).length,
  );
  const allPagePartiallySelected = $derived(
    selectedOnPageCount > 0 && !allPageSelected,
  );

  const selectedEntries = $derived(
    entries.filter((e) => selectedIds.has(e.id)),
  );

  const selectedEntriesHaveEpisodeScope = $derived(
    selectedEntries.some(
      (entry) => entry.episode_id != null || entry.episode_number != null,
    ),
  );

  const selectedTotalBytes = $derived(
    selectedEntries.reduce((acc, e) => acc + (e.estimated_space_bytes ?? 0), 0),
  );

  $effect(() => _mediaFilterStore.save(mediaFilter));
  $effect(() => _sortByStore.save(sortBy));
  $effect(() => _sortOrderStore.save(sortOrder));

  $effect(() => {
    mediaFilter;
    sortBy;
    sortOrder;
    perPage;
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
        per_page: perPage.toString(),
        sort_by: sortBy,
        sort_order: sortOrder,
      });

      if (searchQuery.trim()) params.append("search", searchQuery.trim());
      if (mediaFilter !== "all") params.append("media_type", mediaFilter);

      const response = await get_api<PaginatedResponse<ReclaimCandidateEntry>>(
        `/api/media/candidates?${params.toString()}`,
        signal,
      );
      if (response.total_pages > 0 && page > response.total_pages) {
        await loadCandidates(response.total_pages);
        return;
      }
      data = response;
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

  const sleep = (ms: number) =>
    new Promise((resolve) => window.setTimeout(resolve, ms));

  const candidateJobPayload = (job: BackgroundJobRecord) =>
    (job.payload ?? {}) as Record<string, unknown>;

  const candidateJobOperation = (
    job: BackgroundJobRecord,
  ): "delete" | "move" => {
    const operation = candidateJobPayload(job).operation;
    return operation === "move" ? "move" : "delete";
  };

  const candidateJobResult = (
    job: BackgroundJobRecord,
  ): CandidateFileOpResult | null => {
    const result = candidateJobPayload(job).result;
    return result && typeof result === "object"
      ? (result as CandidateFileOpResult)
      : null;
  };

  const watchCandidateJob = async (
    jobId: number,
    onTerminal?: (job: BackgroundJobRecord) => Promise<void> | void,
  ) => {
    for (let attempt = 0; attempt < 160; attempt += 1) {
      const job = await get_api<BackgroundJobRecord>(
        `/api/tasks/candidate-file-op-jobs/${jobId}`,
      );

      if (
        job.status === BackgroundJobStatus.Completed ||
        job.status === BackgroundJobStatus.Failed ||
        job.status === BackgroundJobStatus.Canceled
      ) {
        if (onTerminal) {
          await onTerminal(job);
        }
        return job;
      }

      await sleep(1500);
    }

    return null;
  };

  const notifyCandidateJobTerminalState = (job: BackgroundJobRecord) => {
    if (job.status === BackgroundJobStatus.Failed) {
      toast.error(
        job.error_message ??
          `${candidateJobOperation(job) === "move" ? "Move" : "Delete"} job failed.`,
      );
      return;
    }

    if (job.status === BackgroundJobStatus.Canceled) {
      toast.error(
        `${candidateJobOperation(job) === "move" ? "Move" : "Delete"} job was canceled.`,
      );
      return;
    }

    const result = candidateJobResult(job);
    const actionPast =
      candidateJobOperation(job) === "move" ? "Moved" : "Deleted";
    if (result && (result.succeeded ?? 0) > 0) {
      toast.success(
        `${actionPast} ${result.succeeded} item${result.succeeded === 1 ? "" : "s"}.`,
      );
    }
    if (result && (result.failed ?? 0) > 0) {
      toast.error(
        `${result.failed} item${result.failed === 1 ? "" : "s"} could not be ${candidateJobOperation(job) === "move" ? "moved" : "deleted"}.`,
      );
    }
    if (!result) {
      toast.success(
        `${candidateJobOperation(job) === "move" ? "Move" : "Delete"} job completed.`,
      );
    }
  };

  const queueCandidateOperation = async (
    operation: "delete" | "move",
    candidateIds: number[],
  ) => {
    const response = await post_api<CandidateOperationQueuedResponse>(
      operation === "move"
        ? "/api/media/candidates/move"
        : "/api/media/candidates/delete",
      { candidate_ids: candidateIds },
    );

    if (response.message) {
      toast.success(response.message);
    }
    if (response.job_id != null) {
      void watchCandidateJob(response.job_id, async (job) => {
        notifyCandidateJobTerminalState(job);
        await loadCandidates(currentPage);
        uiIndicators.invalidate();
      });
    }

    return response;
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
    if (entry.episode_id != null || entry.episode_number != null) {
      toast.error("Episode candidates cannot be moved yet.");
      return;
    }
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

  // after bulk actions, optimistically remove affected rows, then re-fetch
  // the current page so it backfills with additional results when available
  const applyBulkRemoval = async (idsToRemove: Set<number>) => {
    if (!data || idsToRemove.size === 0) return;
    removeCandidateIds(idsToRemove);
    await loadCandidates(currentPage);
  };

  const submitSingleDelete = async () => {
    if (!deleteTarget) return;
    deleteSubmitting = true;
    try {
      await queueCandidateOperation("delete", [deleteTarget.id]);
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
      await queueCandidateOperation("delete", selectedCandidateIds);
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
      await queueCandidateOperation("move", [moveTarget.id]);
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
    if (selectedEntriesHaveEpisodeScope) {
      toast.error("Episode candidates cannot be moved yet.");
      return;
    }
    bulkMoveSubmitting = true;
    try {
      await queueCandidateOperation(
        "move",
        selectedEntries.map((e) => e.id),
      );
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
        (() => {
          if (
            item.media_id !== request.media_id ||
            item.media_type !== request.media_type
          ) {
            return false;
          }

          if (request.media_type === MediaType.Movie) {
            return request.movie_version_id == null
              ? true
              : (item.movie_version_id ?? null) === request.movie_version_id;
          }

          if (request.episode_id != null) {
            return (item.episode_id ?? null) === request.episode_id;
          }

          if (request.season_id != null) {
            return (item.season_id ?? null) === request.season_id;
          }

          return true;
        })()
          ? { ...item, has_pending_request: true }
          : item,
      ),
    };
  };

  interface BulkProtectionRequestItem {
    candidateIds: number[];
    payload: {
      media_type: MediaType;
      media_id: number;
      movie_version_id?: number;
      season_id?: number;
      episode_id?: number;
      reason: string;
      duration_days: number | null;
    };
  }

  const groupBy = <T, K>(items: T[], keyFor: (item: T) => K) => {
    const grouped = new Map<K, T[]>();
    for (const item of items) {
      const key = keyFor(item);
      const group = grouped.get(key) ?? [];
      group.push(item);
      grouped.set(key, group);
    }
    return grouped;
  };

  const buildSeriesBulkProtectionRequests = async (
    seriesId: number,
    entriesForSeries: ReclaimCandidateEntry[],
    durationDays: number | null,
  ): Promise<BulkProtectionRequestItem[]> => {
    const basePayload = {
      media_type: MediaType.Series,
      media_id: seriesId,
      reason: "Admin decision",
      duration_days: durationDays,
    };

    const wholeSeriesEntry = entriesForSeries.find(
      (entry) => entry.season_id == null && entry.episode_id == null,
    );
    if (wholeSeriesEntry) {
      return [
        {
          candidateIds: entriesForSeries.map((entry) => entry.id),
          payload: basePayload,
        },
      ];
    }

    const seasons = await get_api<SeasonWithStatus[]>(
      `/api/media/series/${seriesId}/seasons`,
    );
    const episodes = await get_api<EpisodeWithStatus[]>(
      `/api/media/series/${seriesId}/episodes`,
    );
    const episodesBySeasonId = groupBy(
      episodes,
      (episode) => episode.season_id,
    );
    const selectedSeasonIds = new Set(
      entriesForSeries
        .filter((entry) => entry.season_id != null && entry.episode_id == null)
        .map((entry) => entry.season_id as number),
    );
    const selectedEpisodeIds = new Set(
      entriesForSeries
        .filter((entry) => entry.episode_id != null)
        .map((entry) => entry.episode_id as number),
    );

    const selectedCandidateIdsBySeasonId = new Map<number, number[]>();
    for (const entry of entriesForSeries) {
      if (entry.season_id == null) continue;
      const allIds = selectedCandidateIdsBySeasonId.get(entry.season_id) ?? [];
      allIds.push(entry.id);
      selectedCandidateIdsBySeasonId.set(entry.season_id, allIds);
    }

    const isEpisodeCovered = (episode: EpisodeWithStatus) =>
      selectedEpisodeIds.has(episode.id) ||
      episode.status.is_protected ||
      episode.status.has_pending_request;

    const isSeasonCovered = (season: SeasonWithStatus) => {
      if (
        selectedSeasonIds.has(season.id) ||
        season.status.is_protected ||
        season.status.has_pending_request
      ) {
        return true;
      }
      const seasonEpisodes = episodesBySeasonId.get(season.id) ?? [];
      return (
        seasonEpisodes.length > 0 && seasonEpisodes.every(isEpisodeCovered)
      );
    };

    const allSeriesCovered =
      seasons.length > 0
        ? seasons.every(isSeasonCovered)
        : episodes.length > 0 && episodes.every(isEpisodeCovered);
    if (allSeriesCovered) {
      return [
        {
          candidateIds: entriesForSeries.map((entry) => entry.id),
          payload: basePayload,
        },
      ];
    }

    const requests: BulkProtectionRequestItem[] = [];
    const selectedEntriesBySeasonId = groupBy(
      entriesForSeries.filter((entry) => entry.season_id != null),
      (entry) => entry.season_id as number,
    );

    for (const [seasonId, seasonEntries] of selectedEntriesBySeasonId) {
      const season = seasons.find((item) => item.id === seasonId);
      const hasSelectedSeason = selectedSeasonIds.has(seasonId);
      if (hasSelectedSeason || (season && isSeasonCovered(season))) {
        requests.push({
          candidateIds:
            selectedCandidateIdsBySeasonId.get(seasonId) ??
            seasonEntries.map((entry) => entry.id),
          payload: { ...basePayload, season_id: seasonId },
        });
        continue;
      }

      for (const entry of seasonEntries) {
        if (entry.episode_id == null) continue;
        requests.push({
          candidateIds: [entry.id],
          payload: { ...basePayload, episode_id: entry.episode_id },
        });
      }
    }

    return requests;
  };

  const buildBulkProtectionRequests = async (
    selected: ReclaimCandidateEntry[],
    durationDays: number | null,
  ): Promise<BulkProtectionRequestItem[]> => {
    const requests: BulkProtectionRequestItem[] = [];
    const movieEntries = selected.filter(
      (entry) => entry.media_type === MediaType.Movie,
    );
    const seriesEntriesById = groupBy(
      selected.filter((entry) => entry.media_type === MediaType.Series),
      (entry) => entry.media_id,
    );

    for (const entry of movieEntries) {
      requests.push({
        candidateIds: [entry.id],
        payload: {
          media_type: entry.media_type,
          media_id: entry.media_id,
          movie_version_id: entry.movie_version_id ?? undefined,
          reason: "Admin decision",
          duration_days: durationDays,
        },
      });
    }

    for (const [seriesId, entriesForSeries] of seriesEntriesById) {
      requests.push(
        ...(await buildSeriesBulkProtectionRequests(
          seriesId,
          entriesForSeries,
          durationDays,
        )),
      );
    }

    return requests;
  };

  // Submit bulk protection request for all selected candidates. Duration is determined by the
  // bulkDuration state (either a preset duration, custom number of days, or permanent).
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

    try {
      const requestItems = await buildBulkProtectionRequests(
        selectedEntries,
        durationDays,
      );
      const results = await Promise.allSettled(
        requestItems.map((item) =>
          post_api<ProtectionRequest>("/api/protection-requests", item.payload),
        ),
      );

      const succeededIds = requestItems
        .map((item, idx) => ({ item, result: results[idx] }))
        .filter(({ result }) => result.status === "fulfilled")
        .flatMap(({ item }) => item.candidateIds);
      // const succeeded = succeededIds.length;
      const submitted = results.filter(
        (result) => result.status === "fulfilled",
      ).length;
      const failed = results.length - submitted;

      if (submitted > 0)
        toast.success(
          `Submitted ${submitted} protection request${submitted !== 1 ? "s" : ""}`,
        );
      if (failed > 0)
        toast.error(`${failed} request${failed !== 1 ? "s" : ""} failed`);

      if (succeededIds.length > 0) {
        await applyBulkRemoval(new Set(succeededIds));
      }

      selectedIds = new Set();
      bulkDialogOpen = false;
    } catch (e: any) {
      toast.error(e.message ?? "Failed to submit protection requests.");
    } finally {
      bulkSubmitting = false;
    }
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
    mounted = false;
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
            > and remove the file from disk. Only this episode is affected - the series
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
<Dialog.Root
  open={bulkDialogOpen}
  onOpenChange={(open) => {
    if (!bulkSubmitting) bulkDialogOpen = open;
  }}
>
  <Dialog.Content
    class="sm:max-w-md text-foreground"
    showCloseButton={!bulkSubmitting}
    onInteractOutside={(event) => {
      if (bulkSubmitting) event.preventDefault();
    }}
  >
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
  episodeId={requestTarget?.episode_id ?? null}
  episodeNumber={requestTarget?.episode_number ?? null}
  episodeName={requestTarget?.episode_name ?? null}
  onSuccess={handleRequestSuccess}
/>

<div class="p-2.5 md:p-8">
  <div class="max-w-7xl mx-auto space-y-4">
    <div>
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

      <div class="flex flex-1 flex-col gap-2 sm:flex-row">
        <!-- row 1 on mobile: sort by + sort order -->
        <div class="flex flex-wrap md:flex-nowrap flex-1 gap-2">
          <Select.Root
            type="single"
            value={mediaFilter}
            onValueChange={(v) => {
              mediaFilter =
                v === MediaType.Movie || v === MediaType.Series ? v : "all";
              selectedIds = new Set();
              expandedGroups = new Set();
            }}
          >
            <Select.Trigger class="flex-1 bg-card text-card-foreground">
              {mediaFilter === "all"
                ? "All media"
                : mediaFilter === MediaType.Movie
                  ? "Movies"
                  : "Series"}
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
                label="Series"
                class="text-card-foreground"
              >
                Series
              </Select.Item>
            </Select.Content>
          </Select.Root>

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

    <!-- page level selection control -->
    {#if canBulkSelect && !loading && selectableOnPageCount > 0}
      <div
        class="flex flex-col gap-2 rounded-lg border border-border bg-muted/35 px-4 py-2.5
          sm:flex-row sm:items-center sm:justify-between"
      >
        <label
          class="flex flex-wrap items-center gap-2 text-sm font-medium text-foreground cursor-pointer"
        >
          <input
            type="checkbox"
            checked={allPageSelected}
            indeterminate={allPagePartiallySelected}
            onchange={toggleSelectAll}
            class="cursor-pointer accent-primary"
          />
          Select all on this page
        </label>
        <span class="text-xs text-muted-foreground sm:text-sm">
          {selectedOnPageCount} of {selectableOnPageCount} selected
        </span>
      </div>
    {/if}

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
        <div class="flex flex-wrap gap-2 justify-end">
          <Button
            size="sm"
            class="cursor-pointer"
            onclick={() => (selectedIds = new Set())}
          >
            <Eraser class="size-4" />
            Clear
          </Button>
          {#if isAdmin}
            <!-- protect -->
            <Tooltip.Root>
              <Tooltip.Trigger>
                <Button
                  size="sm"
                  class="cursor-pointer bg-green-600/80 hover:bg-green-600/60"
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
                  disabled={selectedEntriesHaveEpisodeScope}
                  onclick={() => (bulkMoveDialogOpen = true)}
                >
                  <FolderOutput class="size-4" />
                  Move {selectedIds.size}
                </Button>
              </Tooltip.Trigger>
              <Tooltip.Content>
                <p>
                  {selectedEntriesHaveEpisodeScope
                    ? "Episode candidates cannot be moved yet"
                    : "Move to destination folder"}
                </p>
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
      {:else if mediaFilter === "all"}
        <MixedCandidatesView
          rows={displayRows()}
          {canBulkSelect}
          {canDelete}
          {selectedIds}
          {expandedGroups}
          {allPageSelected}
          {toggleSelect}
          {toggleSelectAll}
          {toggleGroupSelect}
          {isGroupAllSelected}
          {isGroupPartialSelected}
          {toggleExpand}
          {openSingleRequest}
          {openSingleDelete}
          {openSingleMove}
          {moveEnabled}
          {formatDate}
          {groupTotalBytes}
        />
      {:else if mediaFilter === MediaType.Movie}
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
          )} of {data.total} results
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
</div>
