<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import DeletionTiming from "./deletion-timing.svelte";
  import {
    scheduledDeletionError,
    scheduledDeletionTime,
    localDateTime,
  } from "$lib/utils/scheduled-deletion-time.js";
  import { formatDateTimeToLocaleString } from "$lib/utils/date";
  import { scheduledDeletionEditRequest } from "$lib/stores/scheduled-deletion-edit";
  import { toast } from "svelte-sonner";
  import type { ScheduledDeletion } from "$lib/types/shared";

  let {
    canManage,
    refreshKey = 0,
    onChanged,
  }: {
    canManage: boolean;
    refreshKey?: number;
    onChanged?: () => void;
  } = $props();
  let schedules = $state<ScheduledDeletion[]>([]);
  let error = $state("");
  let loaded = $state(false);
  let target = $state<ScheduledDeletion | null>(null);
  let action = $state<"reschedule" | "cancel">("reschedule");
  let open = $state(false);
  let listOpen = $state(false);
  const nextAt = $derived(
    schedules
      .filter((item) => item.state === "scheduled")
      .map((item) => item.scheduled_at)
      .sort()[0],
  );
  let value = $state("");
  let busy = $state(false);
  let timer: ReturnType<typeof setInterval> | undefined;
  let alive = true;
  let generation = 0;

  async function load() {
    const current = ++generation;
    try {
      const result = await get_api<ScheduledDeletion[]>(
        "/api/media/scheduled-deletions",
      );
      if (!alive || current !== generation) return;
      const changed = schedules.some((previous) => {
        const next = result.find((item) => item.id === previous.id);
        return !next || next.state !== previous.state;
      });
      schedules = result;
      if (!result.length) listOpen = false;
      error = "";
      if (changed) onChanged?.();
    } catch (e) {
      if (alive && current === generation)
        error =
          e instanceof Error ? e.message : "Failed to load scheduled deletions";
    }
  }
  $effect(() => {
    refreshKey;
    if (loaded) void load();
  });
  onMount(() => {
    loaded = true;
    timer = setInterval(() => void load(), 15000);
  });
  onDestroy(() => {
    alive = false;
    if (timer) clearInterval(timer);
  });

  function edit(schedule: ScheduledDeletion, next: "reschedule" | "cancel") {
    target = schedule;
    action = next;
    value = localDateTime(new Date(schedule.scheduled_at));
    open = true;
  }
  // a candidate row asked to edit its schedule
  $effect(() => {
    const request = $scheduledDeletionEditRequest;
    if (!request) return;
    scheduledDeletionEditRequest.set(null);
    const schedule = schedules.find((item) => item.id === request.scheduleId);
    if (schedule && schedule.state !== "running")
      edit(schedule, request.action);
  });
  async function save() {
    if (!target || busy) return;
    busy = true;
    try {
      await post_api(
        `/api/media/scheduled-deletions/${target.id}/${action}`,
        action === "reschedule"
          ? { scheduled_at: scheduledDeletionTime(value) }
          : {},
      );
      toast.success(
        action === "cancel"
          ? "Scheduled deletion canceled. Normal cleanup rules apply again."
          : "Deletion rescheduled",
      );
      open = false;
      await load();
      onChanged?.();
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "Failed to update scheduled deletion",
      );
    } finally {
      busy = false;
    }
  }
</script>

{#if error}
  <div
    role="alert"
    class="rounded-lg border border-destructive p-3 text-sm text-foreground"
  >
    {error}
    <Button size="sm" variant="outline" onclick={load}>Retry</Button>
  </div>
{/if}
{#if schedules.length}
  <div
    class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-card px-4 py-2 text-sm text-foreground"
  >
    <span>
      <strong>{schedules.length}</strong> scheduled manual deletion{schedules.length ===
      1
        ? ""
        : "s"}{#if nextAt}
        · next: {formatDateTimeToLocaleString(nextAt)}{/if}
    </span>
    <Button size="sm" variant="outline" onclick={() => (listOpen = true)}
      >Manage</Button
    >
  </div>
{/if}

<Dialog.Root bind:open={listOpen}>
  <Dialog.Content
    class="max-h-[90dvh] overflow-y-auto text-foreground sm:max-w-2xl"
  >
    <Dialog.Header>
      <Dialog.Title
        >Scheduled manual deletions ({schedules.length})</Dialog.Title
      >
      <Dialog.Description
        >Reschedule or cancel before execution starts.</Dialog.Description
      >
    </Dialog.Header>
    <ul
      class="max-h-[60dvh] divide-y divide-border overflow-y-auto rounded-md border border-border"
    >
      {#each schedules as schedule (schedule.id)}
        <li class="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div class="min-w-0 flex-1">
            <p class="font-medium">
              {schedule.target.title} · {schedule.target.scope}
            </p>
            <p class="text-sm text-muted-foreground">
              {schedule.target.operation === "move" ? "Move" : "Delete"} on {formatDateTimeToLocaleString(
                schedule.scheduled_at,
              )} · {schedule.state}
            </p>
            {#if schedule.state === "blocked"}<p
                class="text-sm text-muted-foreground"
              >
                Blocked, for example by active playback or protection. It will
                not retry; reschedule to try again.
              </p>{/if}
            {#if schedule.last_error}<p class="text-sm text-destructive">
                {schedule.last_error}
              </p>{/if}
          </div>
          {#if canManage}
            <div class="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={schedule.state === "running"}
                onclick={() => edit(schedule, "reschedule")}>Reschedule</Button
              >
              <Button
                size="sm"
                variant="outline"
                disabled={schedule.state === "running"}
                onclick={() => edit(schedule, "cancel")}
                >Cancel scheduled deletion</Button
              >
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  </Dialog.Content>
</Dialog.Root>

<Dialog.Root
  bind:open
  onOpenChange={(next) => {
    if (!busy) open = next;
  }}
>
  <Dialog.Content
    class="max-h-[90dvh] overflow-y-auto text-foreground"
    showCloseButton={!busy}
    onInteractOutside={(event) => {
      if (busy) event.preventDefault();
    }}
    onEscapeKeydown={(event) => {
      if (busy) event.preventDefault();
    }}
  >
    <Dialog.Header>
      <Dialog.Title
        >{action === "cancel"
          ? "Cancel scheduled deletion?"
          : "Reschedule deletion"}</Dialog.Title
      >
      <Dialog.Description
        >{target?.target.title}{action === "cancel"
          ? ". Normal cleanup rules will apply again, including any earlier automatic deletion date."
          : ". Choose a new deletion date and time."}</Dialog.Description
      >
    </Dialog.Header>
    {#if action === "reschedule"}<DeletionTiming
        mode="later"
        bind:value
        allowNow={false}
      />{/if}
    <Dialog.Footer>
      <Button variant="outline" disabled={busy} onclick={() => (open = false)}
        >Back</Button
      >
      <Button
        disabled={busy ||
          (action === "reschedule" && !!scheduledDeletionError(value))}
        onclick={save}
        >{busy
          ? "Saving…"
          : action === "cancel"
            ? "Cancel scheduled deletion"
            : "Save new time"}</Button
      >
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
