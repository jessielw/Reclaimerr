<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import HistoryIcon from "@lucide/svelte/icons/history";
  import RotateCw from "@lucide/svelte/icons/rotate-cw";
  import Search from "@lucide/svelte/icons/search";
  import { get_api } from "$lib/api";
  import { auth } from "$lib/stores/auth";
  import ErrorBox from "$lib/components/error-box.svelte";
  import CompactPagination from "$lib/components/compact-pagination.svelte";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import {
    BackgroundJobStatus,
    BackgroundJobType,
    MediaType,
    Permission,
    UserRole,
    type BackgroundJobRecord,
    type CandidateFileOpJobItem,
    type CandidateFileOpJobPayload,
    type PaginatedResponse,
    type ReclaimHistoryEntry,
  } from "$lib/types/shared";
  import {
    formatDateTimeToLocaleString,
    formatDistanceToNow,
  } from "$lib/utils/date";
  import { formatFileSize } from "$lib/utils/formatters";
  import {
    createFilterState,
    createPerPageState,
    PER_PAGE_OPTIONS,
  } from "$lib/utils/pagination";
  import { toTitleCase } from "$lib/utils/strings";
  import { toast } from "svelte-sonner";

  const ITEM_PREVIEW_LIMIT = 3;

  const isAdmin = $derived($auth.user?.role === UserRole.Admin);
  const canViewDetailedHistory = $derived(
    $auth.user?.role === UserRole.Admin ||
      ($auth.user?.permissions ?? []).includes(Permission.ManageReclaim) ||
      ($auth.user?.permissions ?? []).includes(Permission.ManageRequests),
  );

  const _historySearchStore = createFilterState("history_search", "");
  const _historyMediaTypeStore = createFilterState("history_media_type", "all");
  const _historySortStore = createFilterState("history_sort_order", "desc");
  const _historyPerPageStore = createPerPageState("history_per_page");
  const _activityStatusStore = createFilterState(
    "history_activity_status_filter",
    "all",
  );
  const _activityTypeStore = createFilterState(
    "history_activity_type_filter",
    "all",
  );
  const _activityPerPageStore = createPerPageState("history_activity_per_page");

  let historyData = $state<PaginatedResponse<ReclaimHistoryEntry> | null>(null);
  let historyLoading = $state(true);
  let historyError = $state("");
  let historyPage = $state(1);
  let historySearch = $state(_historySearchStore.getInitial());
  let historyMediaType = $state<MediaType | "all">(
    _historyMediaTypeStore.getInitial() === "all"
      ? "all"
      : (_historyMediaTypeStore.getInitial() as MediaType),
  );
  let historySortOrder = $state<"asc" | "desc">(
    _historySortStore.getInitial() === "asc" ? "asc" : "desc",
  );
  let historyPerPage = $state(_historyPerPageStore.getInitial());

  let activityData = $state<PaginatedResponse<BackgroundJobRecord> | null>(
    null,
  );
  let activityLoading = $state(false);
  let activityRefreshing = $state(false);
  let activityError = $state("");
  let activityPage = $state(1);
  let activityPerPage = $state(_activityPerPageStore.getInitial());
  let selectedActivityStatus = $state<BackgroundJobStatus | "all">(
    _activityStatusStore.getInitial() === "all"
      ? "all"
      : (_activityStatusStore.getInitial() as BackgroundJobStatus),
  );
  let selectedActivityType = $state<BackgroundJobType | "all">(
    _activityTypeStore.getInitial() === "all"
      ? "all"
      : (_activityTypeStore.getInitial() as BackgroundJobType),
  );

  let mounted = $state(false);
  let historySearchTimer: ReturnType<typeof setTimeout> | null = null;
  let historyAbortController: AbortController | null = null;
  let activityAbortController: AbortController | null = null;
  let activityRefreshInterval: number | null = null;

  const activityItems = $derived(activityData?.items ?? []);
  const totalActivity = $derived(activityData?.total ?? 0);

  const statusOptions = [
    { value: "all", label: "All statuses" },
    { value: BackgroundJobStatus.Pending, label: "Queued" },
    { value: BackgroundJobStatus.Running, label: "Running" },
    { value: BackgroundJobStatus.Completed, label: "Completed" },
    { value: BackgroundJobStatus.Failed, label: "Failed" },
    { value: BackgroundJobStatus.Canceled, label: "Canceled" },
  ] as const;

  const adminTypeOptions = [
    { value: "all", label: "All activity" },
    { value: BackgroundJobType.CandidateFileOp, label: "Reclaim activity" },
    { value: BackgroundJobType.TaskRun, label: "Task runs" },
    { value: BackgroundJobType.ServiceToggle, label: "Service toggles" },
  ] as const;

  const privilegedTypeOptions = [
    { value: BackgroundJobType.CandidateFileOp, label: "Reclaim activity" },
  ] as const;

  const visibleActivityTypeOptions = $derived(
    isAdmin ? adminTypeOptions : privilegedTypeOptions,
  );

  const normalizeSelectedActivityType = () => {
    if (isAdmin) return;
    if (selectedActivityType !== BackgroundJobType.CandidateFileOp) {
      selectedActivityType = BackgroundJobType.CandidateFileOp;
    }
  };

  $effect(() => _historySearchStore.save(historySearch));
  $effect(() => _historyMediaTypeStore.save(historyMediaType));
  $effect(() => _historySortStore.save(historySortOrder));
  $effect(() => _historyPerPageStore.save(historyPerPage));
  $effect(() => _activityStatusStore.save(selectedActivityStatus));
  $effect(() => _activityTypeStore.save(selectedActivityType));
  $effect(() => _activityPerPageStore.save(activityPerPage));

  $effect(() => {
    historyMediaType;
    historySortOrder;
    historyPerPage;
    if (mounted) {
      void loadHistory(1);
    }
  });

  $effect(() => {
    normalizeSelectedActivityType();
    selectedActivityStatus;
    selectedActivityType;
    activityPerPage;
    if (mounted && canViewDetailedHistory) {
      void loadActivity(1);
    }
  });

  const buildHistoryEndpoint = (page: number) => {
    const params = new URLSearchParams({
      page: page.toString(),
      per_page: historyPerPage.toString(),
      sort_order: historySortOrder,
    });
    if (historyMediaType !== "all") params.set("media_type", historyMediaType);
    if (historySearch.trim()) params.set("search", historySearch.trim());
    return `/api/media/reclaim-history?${params.toString()}`;
  };

  const buildActivityEndpoint = (page: number) => {
    const params = new URLSearchParams({
      page: page.toString(),
      per_page: activityPerPage.toString(),
    });
    if (selectedActivityStatus !== "all") {
      params.set("status", selectedActivityStatus);
    }
    if (selectedActivityType !== "all") {
      params.set("job_type", selectedActivityType);
    }
    return `/api/tasks/history?${params.toString()}`;
  };

  const loadHistory = async (page: number = historyPage) => {
    if (historyAbortController) historyAbortController.abort();
    historyAbortController = new AbortController();
    const signal = historyAbortController.signal;
    historyPage = page;
    historyLoading = true;
    historyError = "";

    try {
      const response = await get_api<PaginatedResponse<ReclaimHistoryEntry>>(
        buildHistoryEndpoint(page),
        signal,
      );
      if (response.total_pages > 0 && page > response.total_pages) {
        await loadHistory(response.total_pages);
        return;
      }
      historyData = response;
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      historyError = e.message ?? "Failed to load reclaim history.";
    } finally {
      if (!signal.aborted) {
        historyLoading = false;
      }
    }
  };

  const loadActivity = async (
    page: number = activityPage,
    showRefreshSpinner = false,
  ) => {
    if (!canViewDetailedHistory) {
      activityLoading = false;
      activityData = null;
      activityError = "";
      return;
    }

    if (activityAbortController) activityAbortController.abort();
    activityAbortController = new AbortController();
    const signal = activityAbortController.signal;

    activityPage = page;
    if (showRefreshSpinner) {
      activityRefreshing = true;
    } else {
      activityLoading = true;
    }
    activityError = "";

    try {
      const response = await get_api<PaginatedResponse<BackgroundJobRecord>>(
        buildActivityEndpoint(page),
        signal,
      );
      if (response.total_pages > 0 && page > response.total_pages) {
        await loadActivity(response.total_pages, showRefreshSpinner);
        return;
      }
      activityData = response;
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      activityError = e.message ?? "Failed to load recent activity.";
      if (showRefreshSpinner) {
        toast.error(activityError);
      }
    } finally {
      if (!signal.aborted) {
        activityLoading = false;
        activityRefreshing = false;
      }
    }
  };

  const handleHistorySearch = () => {
    if (historySearchTimer) clearTimeout(historySearchTimer);
    historySearchTimer = setTimeout(() => {
      void loadHistory(1);
    }, 300);
  };

  const candidatePayload = (
    job: BackgroundJobRecord,
  ): CandidateFileOpJobPayload | null => {
    if (job.job_type !== BackgroundJobType.CandidateFileOp) return null;
    const payload = job.payload as CandidateFileOpJobPayload;
    return payload && typeof payload.operation === "string" ? payload : null;
  };

  const candidateItems = (
    job: BackgroundJobRecord,
  ): CandidateFileOpJobItem[] => {
    const payload = candidatePayload(job);
    return Array.isArray(payload?.item_details) ? payload.item_details : [];
  };

  const candidatePreviewItems = (
    job: BackgroundJobRecord,
  ): CandidateFileOpJobItem[] => {
    return candidateItems(job).slice(0, ITEM_PREVIEW_LIMIT);
  };

  const candidateProgress = (job: BackgroundJobRecord) => {
    const payload = candidatePayload(job);
    const progress = payload?.progress;
    return progress && typeof progress.total_items === "number"
      ? progress
      : null;
  };

  const candidatePrimaryLabel = (job: BackgroundJobRecord): string | null => {
    const [firstItem] = candidatePreviewItems(job);
    if (firstItem?.display_label) return firstItem.display_label;
    const payload = candidatePayload(job);
    const [firstLabel] = payload?.item_labels ?? [];
    return typeof firstLabel === "string" ? firstLabel : null;
  };

  const candidateRemainingCount = (job: BackgroundJobRecord): number => {
    const payload = candidatePayload(job);
    if (!payload) return 0;
    const total =
      payload.item_label_total ?? payload.candidate_ids?.length ?? 0;
    const shown =
      candidatePreviewItems(job).length ||
      Math.min(payload.item_labels?.length ?? 0, ITEM_PREVIEW_LIMIT);
    return Math.max(0, total - shown);
  };

  const activityOperationLabel = (job: BackgroundJobRecord): string | null => {
    const payload = candidatePayload(job);
    if (!payload) return null;
    return payload.operation === "move" ? "moved" : "deleted";
  };

  const activityHeadline = (job: BackgroundJobRecord): string => {
    const payload = candidatePayload(job);
    if (!payload) return job.summary ?? "System activity";

    const primaryLabel = candidatePrimaryLabel(job);
    const total = payload.item_label_total ?? payload.candidate_ids.length;
    const operation = payload.operation === "move" ? "move" : "delete";
    const pastTense = payload.operation === "move" ? "moved" : "deleted";

    if (job.status === BackgroundJobStatus.Pending) {
      return primaryLabel && total === 1
        ? `${primaryLabel} queued for ${operation}`
        : `${total} items queued for ${operation}`;
    }
    if (job.status === BackgroundJobStatus.Running) {
      return primaryLabel && total === 1
        ? `${primaryLabel} is being ${pastTense}`
        : `${total} items are being ${pastTense}`;
    }
    if (job.status === BackgroundJobStatus.Completed) {
      return primaryLabel && total === 1
        ? `${primaryLabel} was ${pastTense}`
        : `${total} items were ${pastTense}`;
    }
    if (job.status === BackgroundJobStatus.Failed) {
      return primaryLabel && total === 1
        ? `${primaryLabel} failed to ${operation}`
        : `${total} items failed to ${operation}`;
    }
    if (job.status === BackgroundJobStatus.Canceled) {
      return primaryLabel && total === 1
        ? `${primaryLabel} ${operation} was canceled`
        : `${total} item ${operation} was canceled`;
    }
    return job.summary ?? "Reclaim activity";
  };

  const activityDescription = (job: BackgroundJobRecord): string => {
    const payload = candidatePayload(job);
    if (!payload) return job.summary ?? "";

    const requestedBy = payload.requested_by_username;
    const result = payload.result;
    const pieces: string[] = [];

    if (requestedBy) {
      pieces.push(`Triggered by ${requestedBy}`);
    }
    const progress = candidateProgress(job);
    if (
      progress &&
      job.status === BackgroundJobStatus.Running &&
      progress.total_items > 0
    ) {
      pieces.push(
        `${progress.completed_items} of ${progress.total_items} complete${progress.failed_items ? `, ${progress.failed_items} failed` : ""}`,
      );
    }
    if (result && job.status === BackgroundJobStatus.Completed) {
      pieces.push(
        `${result.succeeded} succeeded${result.failed ? `, ${result.failed} failed` : ""}`,
      );
    }
    return pieces.join(" · ");
  };

  const secondaryLabels = (job: BackgroundJobRecord): string[] => {
    const previewItems = candidatePreviewItems(job);
    if (previewItems.length > 0) {
      return previewItems.slice(1).map((item) => item.display_label);
    }
    const payload = candidatePayload(job);
    return (payload?.item_labels ?? []).slice(1, ITEM_PREVIEW_LIMIT);
  };

  const progressSummary = (job: BackgroundJobRecord): string | null => {
    const progress = candidateProgress(job);
    if (!progress) return null;
    const summary = `${progress.completed_items} / ${progress.total_items} complete`;
    return progress.failed_items
      ? `${summary} · ${progress.failed_items} failed`
      : summary;
  };

  const jobTypeLabel = (jobType: BackgroundJobType) => {
    switch (jobType) {
      case BackgroundJobType.CandidateFileOp:
        return "Reclaim";
      case BackgroundJobType.TaskRun:
        return "Task run";
      case BackgroundJobType.ServiceToggle:
        return "Service toggle";
      default:
        return toTitleCase(jobType, "_");
    }
  };

  const historyActionLabel = (entry: ReclaimHistoryEntry) => {
    if (!canViewDetailedHistory) return "Reclaimed";
    return entry.action === "moved" ? "Moved" : "Deleted";
  };

  const historyActorLabel = (entry: ReclaimHistoryEntry) => {
    if (!canViewDetailedHistory) return null;
    return entry.approved_by ? `by ${entry.approved_by}` : null;
  };

  const historyTypeClasses = (mediaType: string) =>
    mediaType === MediaType.Movie
      ? "bg-blue-500/15 text-blue-400"
      : "bg-emerald-500/15 text-emerald-400";

  const historyAttributeLabels = (entry: ReclaimHistoryEntry): string[] => {
    const attributes = entry.attributes;
    if (!attributes) return [];

    const labels: string[] = [];
    if (attributes.resolution) {
      labels.push(attributes.resolution);
    }
    if (attributes.hdr) {
      labels.push("HDR");
    }
    if (attributes.dolby_vision) {
      labels.push("DV");
    }
    return labels;
  };

  const activityStatusClasses = (status: BackgroundJobStatus) => {
    switch (status) {
      case BackgroundJobStatus.Pending:
        return "bg-amber-500 text-white";
      case BackgroundJobStatus.Running:
        return "bg-blue-500 text-white";
      case BackgroundJobStatus.Completed:
        return "bg-green-600 text-white";
      case BackgroundJobStatus.Failed:
        return "bg-red-600 text-white";
      case BackgroundJobStatus.Canceled:
        return "bg-slate-500 text-white";
      default:
        return "bg-slate-500 text-white";
    }
  };

  const statusLabel = (status: BackgroundJobStatus) =>
    toTitleCase(
      status === BackgroundJobStatus.Pending ? "queued" : status,
      "_",
    );

  onMount(() => {
    mounted = true;
    if (canViewDetailedHistory) {
      activityRefreshInterval = window.setInterval(() => {
        void loadActivity(activityPage, true);
      }, 10000);
    }
  });

  onDestroy(() => {
    mounted = false;
    if (historySearchTimer) clearTimeout(historySearchTimer);
    if (historyAbortController) historyAbortController.abort();
    if (activityAbortController) activityAbortController.abort();
    if (activityRefreshInterval) clearInterval(activityRefreshInterval);
  });
</script>

<div class="p-2.5 md:p-8">
  <div class="max-w-7xl mx-auto space-y-6">
    <div>
      <h1 class="flex items-center gap-3 text-3xl font-bold text-foreground">
        <HistoryIcon class="size-8" />
        History
      </h1>
      <p class="text-muted-foreground">
        Browse reclaimed media and recent cleanup activity.
      </p>
      <p class="mt-1 text-xs text-muted-foreground/80">
        {#if canViewDetailedHistory}
          Detailed reclaim activity is visible because your account can manage
          reclaim or requests.
        {:else}
          Showing a simplified reclaim feed.
        {/if}
      </p>
    </div>

    {#if canViewDetailedHistory}
      <section class="space-y-4">
        <div
          class="flex flex-col gap-3 md:flex-row md:items-end md:justify-between"
        >
          <div>
            <h2 class="text-xl font-semibold text-foreground">
              Recent Activity
            </h2>
            <p class="text-sm text-muted-foreground">
              Queued, running, and recently finished cleanup work.
            </p>
          </div>
          <Button
            type="button"
            variant="outline"
            class="gap-2"
            onclick={() => loadActivity(activityPage, true)}
            disabled={activityRefreshing}
          >
            {#if activityRefreshing}
              <Spinner class="size-4" />
            {:else}
              <RotateCw class="size-4" />
            {/if}
            Refresh
          </Button>
        </div>

        <div
          class="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_140px]"
        >
          <div class="space-y-2">
            <Label class="text-foreground">Status</Label>
            <Select.Root type="single" bind:value={selectedActivityStatus}>
              <Select.Trigger class="w-full text-foreground">
                {statusOptions.find(
                  (opt) => opt.value === selectedActivityStatus,
                )?.label}
              </Select.Trigger>
              <Select.Content>
                {#each statusOptions as option}
                  <Select.Item value={option.value} label={option.label}
                    >{option.label}</Select.Item
                  >
                {/each}
              </Select.Content>
            </Select.Root>
          </div>

          <div class="space-y-2">
            <Label class="text-foreground">Category</Label>
            <Select.Root type="single" bind:value={selectedActivityType}>
              <Select.Trigger class="w-full text-foreground">
                {visibleActivityTypeOptions.find(
                  (opt) => opt.value === selectedActivityType,
                )?.label}
              </Select.Trigger>
              <Select.Content>
                {#each visibleActivityTypeOptions as option}
                  <Select.Item value={option.value} label={option.label}
                    >{option.label}</Select.Item
                  >
                {/each}
              </Select.Content>
            </Select.Root>
          </div>

          <div class="space-y-2">
            <Label class="text-foreground">Page Size</Label>
            <Select.Root
              type="single"
              value={activityPerPage.toString()}
              onValueChange={(value) => {
                const parsed = parseInt(value, 10);
                if (!Number.isNaN(parsed)) {
                  activityPerPage = parsed;
                }
              }}
            >
              <Select.Trigger class="w-full text-foreground"
                >{activityPerPage} / page</Select.Trigger
              >
              <Select.Content>
                {#each PER_PAGE_OPTIONS as option}
                  <Select.Item
                    value={option.toString()}
                    label={`${option} / page`}
                  >
                    {option} / page
                  </Select.Item>
                {/each}
              </Select.Content>
            </Select.Root>
          </div>
        </div>

        <ErrorBox error={activityError} />

        <div class="rounded-2xl border border-border bg-card">
          <!-- {console.log(activityLoading)}
          {console.log(activityData)} -->
          {#if activityLoading && !activityData}
            <div class="p-8 text-center text-muted-foreground">
              <div
                class="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-primary border-r-transparent"
              ></div>
              <p class="mt-4">Loading recent activity...</p>
            </div>
          {:else if activityItems.length === 0}
            <div class="p-8 text-center text-muted-foreground">
              No recent activity matched the current filters.
            </div>
          {:else}
            <div class="divide-y divide-border">
              {#each activityItems as job (job.id)}
                <article class="p-4 sm:p-5">
                  <div
                    class="flex flex-col gap-3 md:flex-row md:items-start md:justify-between"
                  >
                    <div class="space-y-2">
                      <div class="flex flex-wrap items-center gap-2">
                        <Badge class={activityStatusClasses(job.status)}
                          >{statusLabel(job.status)}</Badge
                        >
                        <Badge variant="outline"
                          >{jobTypeLabel(job.job_type)}</Badge
                        >
                        {#if activityOperationLabel(job)}
                          <Badge variant="secondary"
                            >{activityOperationLabel(job)}</Badge
                          >
                        {/if}
                      </div>
                      <h3 class="text-base font-semibold text-foreground">
                        {activityHeadline(job)}
                      </h3>
                      {#if activityDescription(job)}
                        <p class="text-sm text-muted-foreground">
                          {activityDescription(job)}
                        </p>
                      {/if}
                      {#if job.job_type === BackgroundJobType.CandidateFileOp && job.status === BackgroundJobStatus.Running && candidateProgress(job)}
                        <div
                          class="space-y-2 rounded-xl border border-border/70 bg-muted/40 p-3"
                        >
                          <div
                            class="flex flex-col gap-1 text-sm text-muted-foreground sm:flex-row
                              sm:items-center sm:justify-between"
                          >
                            <span>{progressSummary(job)}</span>
                            <span>{candidateProgress(job)?.percent ?? 0}%</span>
                          </div>
                          <div
                            class="h-2 overflow-hidden rounded-full bg-muted"
                          >
                            <div
                              class="h-full rounded-full bg-primary transition-[width] duration-300"
                              style={`width: ${candidateProgress(job)?.percent ?? 0}%`}
                            ></div>
                          </div>
                          {#if candidateProgress(job)?.current_item_label}
                            <p class="text-sm text-foreground">
                              Working on {candidateProgress(job)
                                ?.current_item_label}
                            </p>
                          {/if}
                        </div>
                      {/if}
                      {#if secondaryLabels(job).length > 0 || candidateRemainingCount(job) > 0}
                        <div
                          class="flex flex-wrap gap-2 text-xs text-muted-foreground"
                        >
                          {#each secondaryLabels(job) as label}
                            <span class="rounded-full bg-muted px-2 py-1"
                              >{label}</span
                            >
                          {/each}
                          {#if candidateRemainingCount(job) > 0}
                            <span class="rounded-full bg-muted px-2 py-1"
                              >+{candidateRemainingCount(job)} more</span
                            >
                          {/if}
                        </div>
                      {/if}
                      {#if job.error_message}
                        <p
                          class="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm
                            text-red-300"
                        >
                          {job.error_message}
                        </p>
                      {/if}
                    </div>
                    <div class="text-sm text-muted-foreground md:text-right">
                      <p>{formatDistanceToNow(job.created_at)}</p>
                      <p class="text-xs">
                        {job.completed_at
                          ? `Finished ${formatDateTimeToLocaleString(job.completed_at)}`
                          : job.started_at
                            ? `Started ${formatDateTimeToLocaleString(job.started_at)}`
                            : `Queued ${formatDateTimeToLocaleString(job.created_at)}`}
                      </p>
                    </div>
                  </div>
                </article>
              {/each}
            </div>
          {/if}
        </div>

        {#if !activityLoading && activityData && activityData.total_pages > 1}
          <div
            class="flex flex-wrap items-center justify-center gap-2 md:flex-nowrap md:justify-between"
          >
            <p class="text-sm text-muted-foreground">
              Showing {(activityData.page - 1) * activityData.per_page + 1} to {Math.min(
                activityData.page * activityData.per_page,
                activityData.total,
              )} of {totalActivity} activity item{totalActivity === 1
                ? ""
                : "s"}
            </p>
            <CompactPagination
              currentPage={activityData.page}
              totalPages={activityData.total_pages}
              maxVisiblePages={3}
              onPageChange={loadActivity}
            />
          </div>
        {/if}
      </section>
    {/if}

    <section class="space-y-4">
      <div>
        <h2 class="text-xl font-semibold text-foreground">Reclaim Feed</h2>
        <p class="text-sm text-muted-foreground">
          A running record of media that has already been reclaimed.
        </p>
      </div>

      <div class="flex flex-col gap-2 lg:flex-row">
        <div class="relative flex-1">
          <Search
            class="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            type="text"
            placeholder="Search by title"
            class="pl-10 bg-card"
            bind:value={historySearch}
            oninput={handleHistorySearch}
          />
        </div>
        <div class="grid gap-2 sm:grid-cols-3 lg:w-auto">
          <Select.Root type="single" bind:value={historyMediaType}>
            <Select.Trigger class="w-full bg-card text-card-foreground">
              {historyMediaType === "all"
                ? "All types"
                : historyMediaType === MediaType.Movie
                  ? "Movies"
                  : "Series"}
            </Select.Trigger>
            <Select.Content class="bg-card">
              <Select.Item
                value="all"
                label="All types"
                class="text-card-foreground">All types</Select.Item
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

          <Select.Root type="single" bind:value={historySortOrder}>
            <Select.Trigger class="w-full bg-card text-card-foreground">
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
            onValueChange={(value) => {
              const parsed = parseInt(value, 10);
              if (!Number.isNaN(parsed)) {
                historyPerPage = parsed;
              }
            }}
          >
            <Select.Trigger class="w-full bg-card text-card-foreground"
              >{historyPerPage} / page</Select.Trigger
            >
            <Select.Content class="bg-card">
              {#each PER_PAGE_OPTIONS as option}
                <Select.Item
                  value={option.toString()}
                  label={`${option} / page`}
                  class="text-card-foreground"
                >
                  {option} / page
                </Select.Item>
              {/each}
            </Select.Content>
          </Select.Root>
        </div>
      </div>

      <ErrorBox error={historyError} />

      <div class="rounded-2xl border border-border bg-card">
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
          <div class="divide-y divide-border">
            {#each historyData.items as entry (entry.id)}
              <article class="p-4 sm:p-5">
                <div
                  class="flex flex-col gap-3 md:flex-row md:items-start md:justify-between"
                >
                  <div class="space-y-2">
                    <div class="flex flex-wrap items-center gap-2">
                      <Badge class={historyTypeClasses(entry.media_type)}>
                        {entry.media_type === MediaType.Movie
                          ? "Movie"
                          : "Series"}
                      </Badge>
                      <Badge variant="outline"
                        >{historyActionLabel(entry)}</Badge
                      >
                      {#if entry.size != null}
                        <Badge variant="secondary"
                          >{formatFileSize(entry.size)}</Badge
                        >
                      {/if}
                      {#each historyAttributeLabels(entry) as label}
                        <Badge variant="secondary">{label}</Badge>
                      {/each}
                    </div>
                    <h3 class="text-base font-semibold text-foreground">
                      {entry.name ?? "Unknown title"}
                    </h3>
                    <p class="text-sm text-muted-foreground">
                      {historyActorLabel(entry) ?? "Recorded reclaim activity"}
                    </p>
                  </div>
                  <div class="text-sm text-muted-foreground md:text-right">
                    <p>{formatDistanceToNow(entry.created_at)}</p>
                    <p class="text-xs">
                      {formatDateTimeToLocaleString(entry.created_at)}
                    </p>
                  </div>
                </div>
              </article>
            {/each}
          </div>
        {/if}
      </div>

      {#if !historyLoading && historyData && historyData.total_pages > 1}
        <div
          class="flex flex-wrap items-center justify-center gap-2 md:flex-nowrap md:justify-between"
        >
          <p class="text-sm text-muted-foreground">
            Showing {(historyData.page - 1) * historyData.per_page + 1} to {Math.min(
              historyData.page * historyData.per_page,
              historyData.total,
            )} of {historyData.total} record{historyData.total === 1 ? "" : "s"}
          </p>
          <CompactPagination
            currentPage={historyData.page}
            totalPages={historyData.total_pages}
            maxVisiblePages={3}
            onPageChange={loadHistory}
          />
        </div>
      {/if}
    </section>
  </div>
</div>
