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
  import EditTaskScheduleDialog from "./EditTaskScheduleDialog.svelte";
  import { ScheduleType, TaskStatus } from "$lib/types/shared";

  interface TaskDetails {
    id: string;
    name: string;
    description: string | null;
    next_run: string | null;
    last_run: string | null;
    status: TaskStatus;
    error: string | null;
    trigger_type: string;
    schedule_type: ScheduleType;
    schedule_value: string | null;
    default_schedule_type: ScheduleType;
    default_schedule_value: string;
    enabled: boolean;
    editable: boolean;
  }

  interface TasksResponse {
    tasks: TaskDetails[];
  }

  let tasks = $state<TaskDetails[]>([]);
  let loading = $state(true);
  let refreshInterval: number | null = null;
  let actionInProgress = $state<Record<string, boolean>>({});

  // edit dialog state
  let editDialogOpen = $state(false);
  let editingTask = $state<TaskDetails | null>(null);

  // fetch tasks from API
  const fetchTasks = async () => {
    try {
      const response = await get_api<TasksResponse>("/api/tasks/tasks");
      tasks = response.tasks;
    } catch (error) {
      console.error("Failed to fetch tasks:", error);
      toast.error("Failed to load scheduled tasks");
    } finally {
      loading = false;
    }
  };

  const runTaskNow = async (taskId: string, taskName: string) => {
    actionInProgress[taskId] = true;
    try {
      await post_api(`/api/tasks/tasks/${taskId}/run`, {});
      toast.success(`${taskName} will run shortly`);
      // refresh to show updated status
      await fetchTasks();
    } catch (error) {
      console.error(`Failed to run task ${taskId}:`, error);
      toast.error(`Failed to run ${taskName}`);
    } finally {
      actionInProgress[taskId] = false;
    }
  };

  const openEditDialog = (task: TaskDetails) => {
    if (!task.editable || !task.schedule_type || !task.schedule_value) {
      toast.error("This task is not editable");
      return;
    }
    editingTask = task;
    editDialogOpen = true;
  };

  const handleEditSuccess = () => {
    fetchTasks();
  };

  const formatInterval = (seconds: number): string => {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);

    if (hours > 0) {
      return `Every ${hours} hour${hours > 1 ? "s" : ""}`;
    } else if (minutes > 0) {
      return `Every ${minutes} minute${minutes > 1 ? "s" : ""}`;
    }
    return `Every ${seconds} second${seconds > 1 ? "s" : ""}`;
  };

  // status colors
  const getStatusColor = (status: TaskStatus): string => {
    switch (status) {
      case TaskStatus.Completed:
        return "bg-green-500";
      case TaskStatus.Error:
        return "bg-red-500";
      case TaskStatus.Running:
        return "bg-blue-500";
      case TaskStatus.Disabled:
        return "bg-gray-500";
      case TaskStatus.Scheduled:
        return "bg-yellow-500";
      default:
        return "bg-gray-500";
    }
  };

  // status text
  const getStatusText = (status: TaskStatus): string => {
    return status.charAt(0).toUpperCase() + status.slice(1);
  };

  // get display status - trust the backend's status directly
  const getDisplayStatus = (
    task: TaskDetails,
  ): {
    text: string;
    color: string;
  } => {
    // if disabled, show disabled
    if (!task.enabled) {
      return { text: "Disabled", color: "bg-gray-500" };
    }

    // trust backend status (which includes TTL for completed/failed)
    return {
      text: getStatusText(task.status),
      color: getStatusColor(task.status),
    };
  };

  onMount(() => {
    fetchTasks();
    // poll every 10 seconds to update task status
    refreshInterval = window.setInterval(fetchTasks, 10000);
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
    <!-- show if no tasks -->
  {:else if tasks.length === 0}
    <div class="text-center py-8 text-muted-foreground">
      No scheduled tasks configured
    </div>
    <!-- show tasks -->
  {:else}
    <div class="space-y-4">
      {#each tasks as task (task.id)}
        {@const displayStatus = getDisplayStatus(task)}
        <div class="border rounded-lg p-4 hover:bg-accent transition-colors">
          <div class="flex flex-col sm:flex-row items-start justify-between">
            <div class="flex-1">
              <div class="flex items-center gap-3">
                <h3 class="text-foreground font-semibold">{task.name}</h3>
                <!-- status badge -->
                <Badge class={displayStatus.color}>
                  {displayStatus.text}
                </Badge>
              </div>

              <!-- description -->
              {#if task.description}
                <p class="text-sm text-muted-foreground mt-1">
                  {task.description}
                </p>
              {/if}

              <!-- schedule and run info -->
              <div class="mt-2 space-y-1 text-sm text-muted-foreground">
                <!-- schedule value -->
                {#if task.schedule_value}
                  <span class="flex items-center" title="Schedule"
                    ><CalendarDays class="size-4 text-foreground mr-2" />
                    <!-- interval -->
                    {#if task.schedule_type === ScheduleType.Interval}
                      {formatInterval(parseInt(task.schedule_value))}
                      <!-- cron -->
                    {:else if task.schedule_type === ScheduleType.Cron}
                      {task.schedule_value}
                    {/if}
                  </span>
                {/if}

                <!-- next run -->
                {#if task.status !== TaskStatus.Disabled && task.next_run}
                  <span class="flex items-center"
                    ><ClipboardClock class="size-4 text-foreground mr-2" /> Next
                    run: {formatDistanceToNow(task.next_run)}</span
                  >
                {/if}

                <!-- last run info -->
                <span class="flex items-center"
                  ><Hourglass
                    class="size-4 text-foreground mr-2"
                  />{task.last_run
                    ? formatDistanceToNow(task.last_run)
                    : "Never run"}</span
                >

                <!-- error info -->
                {#if task.error}
                  <p class="flex items-center">
                    <OctagonX
                      class="size-4 text-destructive mr-2"
                    />{task.error}
                  </p>
                {/if}
              </div>
            </div>

            <div class="flex gap-2 ml-0 sm:ml-4 mt-4 sm:mt-0">
              {#if task.editable}
                <Button
                  size="sm"
                  class="cursor-pointer"
                  onclick={() => openEditDialog(task)}
                  disabled={actionInProgress[task.id]}
                  title="Edit schedule"
                >
                  <Pencil class="h-4 w-4" />
                </Button>
              {/if}

              {#if task.enabled}
                <Button
                  size="sm"
                  class="cursor-pointer"
                  onclick={() => runTaskNow(task.id, task.name)}
                  disabled={actionInProgress[task.id]}
                  title="Run now"
                >
                  {#if actionInProgress[task.id]}
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
{#if editingTask && editingTask.schedule_type && editingTask.schedule_value}
  <EditTaskScheduleDialog
    bind:open={editDialogOpen}
    taskId={editingTask.id}
    taskName={editingTask.name}
    scheduleType={editingTask.schedule_type}
    scheduleValue={editingTask.schedule_value}
    defaultScheduleType={editingTask.default_schedule_type}
    defaultScheduleValue={editingTask.default_schedule_value}
    enabled={editingTask.enabled}
    onClose={() => (editDialogOpen = false)}
    onSuccess={handleEditSuccess}
  />
{/if}
