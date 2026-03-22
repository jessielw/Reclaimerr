<script lang="ts">
  import { onDestroy, onMount, type Component } from "svelte";
  import RotateCw from "@lucide/svelte/icons/rotate-cw";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
  import { get_api } from "$lib/api";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { formatDistanceToNow } from "$lib/utils/date";
  import {
    BackgroundJobStatus,
    BackgroundJobType,
    type BackgroundJobRecord,
  } from "$lib/types/shared";
  import { toTitleCase } from "$lib/utils/strings";
  import { toast } from "svelte-sonner";

  interface Props {
    svgIcon: Component | null;
  }

  interface BackgroundJobsResponse {
    jobs: BackgroundJobRecord[];
  }

  let { svgIcon }: Props = $props();

  let jobs = $state<BackgroundJobRecord[]>([]);
  let loading = $state(true);
  let refreshing = $state(false);
  let refreshInterval: number | null = null;
  let selectedStatus = $state<BackgroundJobStatus | "all">("all");
  let selectedType = $state<BackgroundJobType | "all">("all");

  const statusOptions = [
    { value: "all", label: "All statuses" },
    { value: BackgroundJobStatus.Pending, label: "Queued" },
    { value: BackgroundJobStatus.Running, label: "Running" },
    { value: BackgroundJobStatus.Completed, label: "Completed" },
    { value: BackgroundJobStatus.Failed, label: "Failed" },
    { value: BackgroundJobStatus.Canceled, label: "Canceled" },
  ] as const;

  const typeOptions = [
    { value: "all", label: "All job types" },
    { value: BackgroundJobType.TaskRun, label: "Task runs" },
    { value: BackgroundJobType.ServiceToggle, label: "Service toggles" },
  ] as const;

  const buildEndpoint = () => {
    const params = new URLSearchParams({ limit: "50" });
    if (selectedStatus !== "all") params.set("status", selectedStatus);
    if (selectedType !== "all") params.set("job_type", selectedType);
    return `/api/tasks/background-jobs?${params.toString()}`;
  };

  const fetchJobs = async (showSpinner = false) => {
    if (showSpinner) {
      refreshing = true;
    }

    try {
      const response = await get_api<BackgroundJobsResponse>(buildEndpoint());
      jobs = response.jobs;
    } catch (error) {
      console.error("Failed to fetch background jobs:", error);
      toast.error("Failed to load background jobs");
    } finally {
      loading = false;
      refreshing = false;
    }
  };

  const onFilterChange = async () => {
    loading = true;
    await fetchJobs();
  };

  const getStatusClasses = (status: BackgroundJobStatus) => {
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

  const toLabel = (value: string) =>
    toTitleCase(value === BackgroundJobStatus.Pending ? "Queued" : value, "_");

  onMount(() => {
    fetchJobs();
    refreshInterval = window.setInterval(() => {
      fetchJobs();
    }, 10000);
  });

  onDestroy(() => {
    if (refreshInterval) {
      clearInterval(refreshInterval);
    }
  });
</script>

<div class="space-y-6">
  <div class="flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
    <div class="w-full">
      <div class="flex justify-between w-full">
        <h2
          class="flex items-center gap-3 text-xl font-semibold text-foreground"
        >
          {#if svgIcon}
            {@const Icon = svgIcon}
            <Icon class="size-5" aria-hidden="true" />
          {/if}
          <span class="align-middle">Background Jobs</span>
        </h2>
        <Button
          class="cursor-pointer gap-2"
          onclick={() => fetchJobs(true)}
          disabled={refreshing}
        >
          {#if refreshing}
            <Spinner class="size-4" />
          {:else}
            <RotateCw class="size-4" />
          {/if}
          Refresh
        </Button>
      </div>
      <p class="mt-1 text-sm text-muted-foreground">
        Inspect worker-owned jobs such as queued task runs and service toggles.
      </p>
      <p class="mt-2 text-xs text-muted-foreground/80">
        Queued jobs are already enqueued and waiting for the single worker.
        Future APScheduler runs still show up on the Tasks page instead.
      </p>
    </div>
  </div>

  <div class="grid gap-3 md:grid-cols-2">
    <!-- status -->
    <div class="space-y-2">
      <Label class="text-foreground">Status</Label>
      <Select.Root
        type="single"
        bind:value={selectedStatus}
        onValueChange={onFilterChange}
      >
        <Select.Trigger class="w-full text-foreground">
          {statusOptions.find((opt) => opt.value === selectedStatus)?.label}
        </Select.Trigger>
        <Select.Content>
          {#each statusOptions as option}
            <Select.Item
              value={option.value}
              label={option.label}
              class="text-foreground"
            >
              {option.label}
            </Select.Item>
          {/each}
        </Select.Content>
      </Select.Root>
    </div>

    <!-- job type -->
    <div class="space-y-2">
      <Label class="text-foreground">Job Type</Label>
      <Select.Root
        type="single"
        bind:value={selectedType}
        onValueChange={onFilterChange}
      >
        <Select.Trigger class="w-full text-foreground">
          {typeOptions.find((opt) => opt.value === selectedType)?.label}
        </Select.Trigger>
        <Select.Content>
          {#each typeOptions as option}
            <Select.Item
              value={option.value}
              label={option.label}
              class="text-foreground"
            >
              {option.label}
            </Select.Item>
          {/each}
        </Select.Content>
      </Select.Root>
    </div>
  </div>

  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner />
    </div>
  {:else if jobs.length === 0}
    <div
      class="rounded-lg border border-dashed border-border p-8 text-center text-muted-foreground"
    >
      No background jobs matched the current filters.
    </div>
  {:else}
    <div class="space-y-4">
      {#each jobs as job (job.id)}
        <div
          class="rounded-lg border border-border p-4 transition-colors hover:bg-accent/40"
        >
          <div
            class="flex flex-col gap-3 md:flex-row md:items-start md:justify-between"
          >
            <div class="space-y-2">
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="font-semibold text-foreground">#{job.id}</h3>
                <Badge class={getStatusClasses(job.status)}
                  >{toLabel(job.status)}</Badge
                >
                <Badge variant="secondary">{toLabel(job.job_type)}</Badge>
              </div>

              <p class="text-sm text-foreground/90">
                {job.summary ?? "No summary available"}
              </p>

              <div class="space-y-1 text-sm text-muted-foreground">
                <p>Queued {formatDistanceToNow(job.created_at)}</p>
                <p>Scheduled {formatDistanceToNow(job.scheduled_at)}</p>
                {#if job.started_at}
                  <p>Started {formatDistanceToNow(job.started_at)}</p>
                {/if}
                {#if job.completed_at}
                  <p>Completed {formatDistanceToNow(job.completed_at)}</p>
                {/if}
                {#if job.claimed_by}
                  <p>Worker: {job.claimed_by}</p>
                {/if}
                {#if job.dedupe_key}
                  <p>Dedupe key: {job.dedupe_key}</p>
                {/if}
                <p>Attempts: {job.attempts} / {job.max_attempts}</p>
              </div>
            </div>

            {#if job.error_message}
              <div
                class="flex max-w-md items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200"
              >
                <TriangleAlert class="mt-0.5 size-4 shrink-0" />
                <span>{job.error_message}</span>
              </div>
            {/if}
          </div>

          <details class="mt-3 rounded-md bg-background/70 p-3">
            <summary class="cursor-pointer text-sm font-medium text-foreground">
              Payload
            </summary>
            <pre
              class="mt-3 overflow-x-auto text-xs text-muted-foreground">{JSON.stringify(
                job.payload,
                null,
                2,
              )}</pre>
          </details>
        </div>
      {/each}
    </div>
  {/if}
</div>
