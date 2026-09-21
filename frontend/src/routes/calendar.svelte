<script lang="ts">
  import { onMount } from "svelte";
  import { link } from "svelte-spa-router";
  import CalendarDays from "@lucide/svelte/icons/calendar-days";
  import ChevronLeft from "@lucide/svelte/icons/chevron-left";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import RotateCw from "@lucide/svelte/icons/rotate-cw";
  import { get_api } from "$lib/api";
  import ErrorBox from "$lib/components/error-box.svelte";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { formatFileSize } from "$lib/utils/formatters";
  import { formatDateTimeToLocaleString } from "$lib/utils/date";
  import type { CalendarResponse, CalendarDay } from "$lib/types/shared";

  type ViewMode = "month" | "week";

  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  let view = $state<ViewMode>("month");
  // anchor is always local midnight of a day inside the visible range
  let anchor = $state(startOfDay(new Date()));
  let data = $state<CalendarResponse | null>(null);
  let loading = $state(true);
  let error = $state("");
  let selectedKey = $state<string | null>(null);

  function startOfDay(value: Date): Date {
    return new Date(value.getFullYear(), value.getMonth(), value.getDate());
  }

  /** Days back to the Monday on or before `value`. */
  function daysSinceMonday(value: Date): number {
    return (value.getDay() + 6) % 7;
  }

  function addDays(value: Date, days: number): Date {
    const next = new Date(value);
    next.setDate(next.getDate() + days);
    return startOfDay(next);
  }

  /** Local YYYY-MM-DD. toISOString() would shift the day in any zone west of UTC. */
  function dayKey(value: Date): string {
    const month = `${value.getMonth() + 1}`.padStart(2, "0");
    const day = `${value.getDate()}`.padStart(2, "0");
    return `${value.getFullYear()}-${month}-${day}`;
  }

  /** The grid always starts on a Monday and holds whole weeks. */
  const gridStart = $derived.by(() => {
    if (view === "week") return addDays(anchor, -daysSinceMonday(anchor));
    const firstOfMonth = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
    return addDays(firstOfMonth, -daysSinceMonday(firstOfMonth));
  });

  const gridDays = $derived.by(() => {
    if (view === "week") return 7;
    const lastOfMonth = new Date(
      anchor.getFullYear(),
      anchor.getMonth() + 1,
      0,
    );
    const span =
      Math.round(
        (startOfDay(lastOfMonth).getTime() - gridStart.getTime()) / 86400000,
      ) + 1;
    return Math.ceil(span / 7) * 7;
  });

  const gridEnd = $derived(addDays(gridStart, gridDays - 1));

  const cells = $derived.by(() =>
    Array.from({ length: gridDays }, (_, index) => addDays(gridStart, index)),
  );

  const byDay = $derived.by(() => {
    const map = new Map<string, CalendarDay>();
    for (const day of data?.days ?? []) map.set(day.date, day);
    return map;
  });

  const todayKey = dayKey(new Date());

  const heading = $derived.by(() => {
    if (view === "month") {
      return anchor.toLocaleDateString(undefined, {
        month: "long",
        year: "numeric",
      });
    }
    const weekStart = gridStart;
    const weekEnd = addDays(gridStart, 6);
    const opts: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };
    return `${weekStart.toLocaleDateString(undefined, opts)} - ${weekEnd.toLocaleDateString(
      undefined,
      { ...opts, year: "numeric" },
    )}`;
  });

  const selectedDay = $derived.by(() =>
    selectedKey ? (byDay.get(selectedKey) ?? null) : null,
  );

  async function load() {
    loading = true;
    error = "";
    try {
      const params = new URLSearchParams({
        start: dayKey(gridStart),
        end: dayKey(gridEnd),
        // negated so the server reads it as "minutes to add to UTC"
        tz_offset_minutes: String(-new Date().getTimezoneOffset()),
      });
      data = await get_api<CalendarResponse>(`/api/calendar?${params}`);
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to load the calendar";
      data = null;
    } finally {
      loading = false;
    }
  }

  function step(direction: -1 | 1) {
    if (view === "week") {
      anchor = addDays(anchor, direction * 7);
    } else {
      anchor = startOfDay(
        new Date(anchor.getFullYear(), anchor.getMonth() + direction, 1),
      );
    }
    selectedKey = null;
    void load();
  }

  function goToToday() {
    anchor = startOfDay(new Date());
    selectedKey = null;
    void load();
  }

  function setView(next: ViewMode) {
    if (view === next) return;
    view = next;
    selectedKey = null;
    void load();
  }

  function select(key: string) {
    const day = byDay.get(key);
    if (!day) return;
    selectedKey = selectedKey === key ? null : key;
  }

  function scopeLabel(scope: string): string {
    if (scope === "version") return "Version";
    return scope.charAt(0).toUpperCase() + scope.slice(1);
  }

  onMount(load);
</script>

<div class="p-2.5 md:p-8">
  <div class="max-w-7xl mx-auto space-y-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1
          class="text-3xl font-bold text-foreground flex items-center gap-2 flex-wrap"
        >
          <CalendarDays class="h-7 w-7 text-primary" />
          Calendar
        </h1>
        <p class="text-muted-foreground">
          When Reclaimerr is due to delete or move the media it has flagged
        </p>
      </div>
      <Button variant="outline" size="sm" onclick={load} disabled={loading}>
        <RotateCw class="h-4 w-4 {loading ? 'animate-spin' : ''}" />
        Refresh
      </Button>
    </div>

    <ErrorBox {error} />

    <div class="bg-card rounded-lg border border-border">
      <div
        class="flex flex-wrap items-center justify-between gap-3 border-b border-border p-3"
      >
        <div class="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            onclick={() => step(-1)}
            aria-label="Previous"
          >
            <ChevronLeft class="h-4 w-4" />
          </Button>
          <span class="min-w-40 text-center font-semibold text-foreground">
            {heading}
          </span>
          <Button
            variant="ghost"
            size="icon"
            onclick={() => step(1)}
            aria-label="Next"
          >
            <ChevronRight class="h-4 w-4" />
          </Button>
          <Button variant="outline" size="sm" class="ml-2" onclick={goToToday}>
            Today
          </Button>
        </div>

        <div class="flex items-center gap-3">
          {#if data && data.total_items > 0}
            <span class="text-xs text-muted-foreground">
              {data.total_items} scheduled · {formatFileSize(data.total_bytes)}
            </span>
          {/if}
          <div class="flex rounded-md border border-border overflow-hidden">
            <button
              class="px-3 py-1 text-xs transition-colors cursor-pointer {view ===
              'month'
                ? 'bg-primary text-primary-foreground'
                : 'bg-transparent text-muted-foreground hover:bg-secondary'}"
              onclick={() => setView("month")}
            >
              Month
            </button>
            <button
              class="px-3 py-1 text-xs transition-colors cursor-pointer {view ===
              'week'
                ? 'bg-primary text-primary-foreground'
                : 'bg-transparent text-muted-foreground hover:bg-secondary'}"
              onclick={() => setView("week")}
            >
              Week
            </button>
          </div>
        </div>
      </div>

      <div class="grid grid-cols-7 border-b border-border">
        {#each WEEKDAYS as weekday (weekday)}
          <div
            class="px-2 py-1.5 text-center text-[11px] font-medium uppercase tracking-wide text-muted-foreground"
          >
            {weekday}
          </div>
        {/each}
      </div>

      <div class="grid grid-cols-7">
        {#each cells as cell (cell.getTime())}
          {@const key = dayKey(cell)}
          {@const day = byDay.get(key)}
          {@const outside =
            view === "month" && cell.getMonth() !== anchor.getMonth()}
          <button
            class="min-h-20 md:min-h-24 border-b border-r border-border p-1.5 text-left align-top transition-colors
              last:border-r-0 disabled:cursor-default
              {outside ? 'opacity-40' : ''}
              {day ? 'cursor-pointer hover:bg-secondary/60' : ''}
              {selectedKey === key ? 'bg-secondary' : ''}"
            disabled={!day}
            onclick={() => select(key)}
          >
            <div class="flex items-center justify-between gap-1">
              <span
                class="inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1 text-xs
                  {key === todayKey
                  ? 'bg-primary text-primary-foreground font-semibold'
                  : 'text-muted-foreground'}"
              >
                {cell.getDate()}
              </span>
              {#if day}
                <Badge variant="secondary" class="h-5 px-1.5 text-[10px]">
                  {day.item_count}
                </Badge>
              {/if}
            </div>
            {#if day}
              <div class="mt-1 space-y-0.5">
                <p
                  class="truncate text-[11px] text-muted-foreground"
                  title={formatFileSize(day.total_bytes)}
                >
                  {formatFileSize(day.total_bytes)}
                </p>
                {#each day.items.slice(0, 2) as item (item.candidate_id)}
                  <p
                    class="truncate text-[11px] text-foreground"
                    title={item.title}
                  >
                    {item.title}
                  </p>
                {/each}
                {#if day.item_count > 2}
                  <p class="text-[11px] text-muted-foreground">
                    +{day.item_count - 2} more
                  </p>
                {/if}
              </div>
            {/if}
          </button>
        {/each}
      </div>
    </div>

    {#if loading}
      <p class="text-sm text-muted-foreground">Loading calendar...</p>
    {:else if data && data.total_items === 0}
      <div
        class="bg-card rounded-lg border border-border p-8 text-center text-muted-foreground"
      >
        Nothing is scheduled in this range. Candidates appear here once a rule
        with automatic deletion has flagged them.
      </div>
    {/if}

    {#if selectedDay}
      <div class="bg-card rounded-lg border border-border">
        <div
          class="flex flex-wrap items-center justify-between gap-2 border-b border-border p-3"
        >
          <h2 class="font-semibold text-foreground">
            {new Date(`${selectedDay.date}T00:00:00`).toLocaleDateString(
              undefined,
              { weekday: "long", month: "long", day: "numeric" },
            )}
          </h2>
          <span class="text-xs text-muted-foreground">
            {selectedDay.item_count} scheduled · {formatFileSize(
              selectedDay.total_bytes,
            )}
          </span>
        </div>
        <ul class="divide-y divide-border">
          {#each selectedDay.items as item (item.candidate_id)}
            <li
              class="flex flex-wrap items-center justify-between gap-2 px-3 py-2"
            >
              <div class="min-w-0">
                <p class="truncate text-sm text-foreground">
                  {item.title}{item.year ? ` (${item.year})` : ""}
                </p>
                <p class="text-xs text-muted-foreground">
                  {formatDateTimeToLocaleString(item.eligible_at)}
                </p>
              </div>
              <div class="flex items-center gap-1.5">
                <Badge variant="outline" class="text-[10px]">
                  {scopeLabel(item.scope)}
                </Badge>
                <Badge
                  variant={item.operation === "move"
                    ? "secondary"
                    : "destructive"}
                  class="text-[10px]"
                >
                  {item.operation === "move" ? "Move" : "Delete"}
                </Badge>
                {#if item.state === "postponed"}
                  <Badge variant="secondary" class="text-[10px]">
                    Postponed
                  </Badge>
                {/if}
                <span class="text-xs text-muted-foreground">
                  {formatFileSize(item.estimated_space_bytes)}
                </span>
              </div>
            </li>
          {/each}
        </ul>
        {#if selectedDay.truncated}
          <p
            class="border-t border-border px-3 py-2 text-xs text-muted-foreground"
          >
            Showing the first {selectedDay.items.length} of {selectedDay.item_count}.
            <a href="/candidates" use:link class="underline">
              Open Candidates
            </a>
            to see them all.
          </p>
        {/if}
      </div>
    {/if}
  </div>
</div>
