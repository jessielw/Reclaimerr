<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import Play from "@lucide/svelte/icons/play";
  import Pencil from "@lucide/svelte/icons/pencil";
  import CalendarDays from "@lucide/svelte/icons/calendar-days";
  import ClipboardClock from "@lucide/svelte/icons/clipboard-clock";
  import Hourglass from "@lucide/svelte/icons/hourglass";
  import OctagonX from "@lucide/svelte/icons/octagon-x";
  import { toast } from "svelte-sonner";
  import { formatDistanceToNow } from "$lib/utils/date";
  import EditJobScheduleDialog from "./EditJobScheduleDialog.svelte";
  import { ScheduleType, JobStatus } from "$lib/types/shared";

  interface JobDetails {
    id: string;
    name: string;
    description: string | null;
    next_run: string | null;
    last_run: string | null;
    status: JobStatus;
    error: string | null;
    trigger_type: string;
    schedule_type: ScheduleType;
    schedule_value: string | null;
    default_schedule_type: ScheduleType;
    default_schedule_value: string;
    enabled: boolean;
    editable: boolean;
  }

  interface JobsResponse {
    jobs: JobDetails[];
  }

  let jobs = $state<JobDetails[]>([]);
  let loading = $state(true);
  let refreshInterval: number | null = null;
  let actionInProgress = $state<Record<string, boolean>>({});

  // Edit dialog state
  let editDialogOpen = $state(false);
  let editingJob = $state<JobDetails | null>(null);

  // fetch jobs from API
  async function fetchJobs() {
    try {
      const response = await get_api<JobsResponse>("/api/tasks/jobs");
      // console.log("Fetched jobs:", response.jobs);
      jobs = response.jobs;
    } catch (error) {
      console.error("Failed to fetch jobs:", error);
      toast.error("Failed to load scheduled tasks");
    } finally {
      loading = false;
    }
  }

  async function runJobNow(jobId: string, jobName: string) {
    actionInProgress[jobId] = true;
    try {
      await post_api(`/api/tasks/jobs/${jobId}/run`, {});
      toast.success(`${jobName} will run shortly`);
      // Refresh to show updated status
      await fetchJobs();
    } catch (error) {
      console.error(`Failed to run job ${jobId}:`, error);
      toast.error(`Failed to run ${jobName}`);
    } finally {
      actionInProgress[jobId] = false;
    }
  }

  function openEditDialog(job: JobDetails) {
    if (!job.editable || !job.schedule_type || !job.schedule_value) {
      toast.error("This job is not editable");
      return;
    }
    editingJob = job;
    editDialogOpen = true;
  }

  function handleEditSuccess() {
    fetchJobs();
  }

  function formatInterval(seconds: number): string {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);

    if (hours > 0) {
      return `Every ${hours} hour${hours > 1 ? "s" : ""}`;
    } else if (minutes > 0) {
      return `Every ${minutes} minute${minutes > 1 ? "s" : ""}`;
    }
    return `Every ${seconds} second${seconds > 1 ? "s" : ""}`;
  }

  // status colors
  function getStatusColor(status: JobStatus): string {
    switch (status) {
      case JobStatus.Success:
        return "bg-green-500";
      case JobStatus.Error:
        return "bg-red-500";
      case JobStatus.Running:
        return "bg-blue-500";
      case JobStatus.Disabled:
        return "bg-gray-500";
      case JobStatus.Scheduled:
        return "bg-yellow-500";
      default:
        return "bg-gray-500";
    }
  }

  // status text
  function getStatusText(status: JobStatus): string {
    return status.charAt(0).toUpperCase() + status.slice(1);
  }

  onMount(() => {
    fetchJobs();
    // Poll every 10 seconds to update job status
    refreshInterval = window.setInterval(fetchJobs, 10000);
  });

  onDestroy(() => {
    if (refreshInterval) {
      clearInterval(refreshInterval);
    }
  });
</script>

<div class="space-y-6">
  <!-- header -->
  <div class="flex flex-col">
    <h2 class="text-2xl text-foreground font-semibold">Scheduled Tasks</h2>
    <p class="text-sm text-muted-foreground mt-1">
      Manage and monitor automated tasks
    </p>
  </div>

  <!-- check if loading -->
  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner />
    </div>
    <!-- show if no jobs -->
  {:else if jobs.length === 0}
    <div class="text-center py-8 text-muted-foreground">
      No scheduled tasks configured
    </div>
    <!-- show jobs -->
  {:else}
    <div class="space-y-4">
      {#each jobs as job (job.id)}
        <div class="border rounded-lg p-4 hover:bg-accent transition-colors">
          <div class="flex flex-col sm:flex-row items-start justify-between">
            <div class="flex-1">
              <div class="flex items-center gap-3">
                <h3 class="text-foreground font-semibold">{job.name}</h3>
                <!-- status badge -->
                <Badge class={getStatusColor(job.status)}>
                  {job.enabled ? getStatusText(job.status) : "Disabled"}
                </Badge>
              </div>

              <!-- description -->
              {#if job.description}
                <p class="text-sm text-muted-foreground mt-1">
                  {job.description}
                </p>
              {/if}

              <!-- schedule and run info -->
              <div class="mt-2 space-y-1 text-sm text-muted-foreground">
                <!-- schedule value -->
                {#if job.schedule_value}
                  <span class="flex items-center" title="Schedule"
                    ><CalendarDays class="size-4 text-foreground mr-2" />
                    <!-- interval -->
                    {#if job.schedule_type === ScheduleType.Interval}
                      {formatInterval(parseInt(job.schedule_value))}
                      <!-- cron -->
                    {:else if job.schedule_type === ScheduleType.Cron}
                      {job.schedule_value}
                    {/if}
                  </span>
                {/if}

                <!-- next run -->
                {#if job.status !== JobStatus.Disabled && job.next_run}
                  <span class="flex items-center"
                    ><ClipboardClock class="size-4 text-foreground mr-2" /> Next
                    run: {formatDistanceToNow(job.next_run)}</span
                  >
                {/if}

                <!-- last run info -->
                <span class="flex items-center"
                  ><Hourglass
                    class="size-4 text-foreground mr-2"
                  />{job.last_run
                    ? formatDistanceToNow(job.last_run)
                    : "Never run"}</span
                >

                <!-- error info -->
                {#if job.error}
                  <p class="flex items-center">
                    <OctagonX class="size-4 text-destructive mr-2" />{job.error}
                  </p>
                {/if}
              </div>
            </div>

            <div class="flex gap-2 ml-0 sm:ml-4 mt-4 sm:mt-0">
              {#if job.editable}
                <Button
                  size="sm"
                  class="cursor-pointer"
                  onclick={() => openEditDialog(job)}
                  disabled={actionInProgress[job.id]}
                  title="Edit schedule"
                >
                  <Pencil class="h-4 w-4" />
                </Button>
              {/if}

              {#if job.enabled}
                <Button
                  size="sm"
                  class="cursor-pointer"
                  onclick={() => runJobNow(job.id, job.name)}
                  disabled={actionInProgress[job.id]}
                  title="Run now"
                >
                  {#if actionInProgress[job.id]}
                    <Spinner class="h-4 w-4" />
                  {:else}
                    <Play class="h-4 w-4" />
                  {/if}
                </Button>
              {/if}
            </div>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

<!-- edit schedule dialog -->
{#if editingJob && editingJob.schedule_type && editingJob.schedule_value}
  <EditJobScheduleDialog
    bind:open={editDialogOpen}
    jobId={editingJob.id}
    jobName={editingJob.name}
    scheduleType={editingJob.schedule_type}
    scheduleValue={editingJob.schedule_value}
    defaultScheduleType={editingJob.default_schedule_type}
    defaultScheduleValue={editingJob.default_schedule_value}
    enabled={editingJob.enabled}
    onClose={() => (editDialogOpen = false)}
    onSuccess={handleEditSuccess}
  />
{/if}
