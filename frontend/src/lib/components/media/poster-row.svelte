<script lang="ts" module>
  import type { MediaType } from "$lib/types/shared";

  export type PosterRowEntry<T> = {
    key: string;
    mediaType: MediaType;
    title: string;
    subtitle: string;
    posterUrl: string | null;
    tooltip: string[];
    data: T;
  };
</script>

<script lang="ts" generics="T">
  import { onMount, untrack } from "svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import * as Select from "$lib/components/ui/select/index.js";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import ChevronDown from "@lucide/svelte/icons/chevron-down";
  import { createFilterState } from "$lib/utils/pagination";

  const COUNT_OPTIONS = [6, 12, 24];
  // matches the dashboard's own stats refresh
  const REFRESH_MS = 60000;

  interface Props {
    title: string;
    /** Prefix for the per-browser count and collapsed preferences. */
    storageKey: string;
    emptyText: string;
    fetchEntries: (count: number) => Promise<PosterRowEntry<T>[]>;
    onSelect: (entry: PosterRowEntry<T>) => void;
  }
  let { title, storageKey, emptyText, fetchEntries, onSelect }: Props =
    $props();

  // storageKey is fixed at the call site and never changes, so capturing it
  // once with untrack is intentional
  const _prefix = untrack(() => storageKey);
  const _countStore = createFilterState(
    `${_prefix}_count`,
    12,
    (v): v is number => COUNT_OPTIONS.includes(v as number),
  );
  const _collapsedStore = createFilterState(`${_prefix}_collapsed`, false);
  let count = $state(_countStore.getInitial());
  let collapsed = $state(_collapsedStore.getInitial());
  $effect(() => _countStore.save(count));
  $effect(() => _collapsedStore.save(collapsed));

  let entries = $state<PosterRowEntry<T>[]>([]);
  let loading = $state(true);
  let error = $state("");

  let loadSeq = 0;
  /** Reloads the row and returns the fresh entries. */
  export const reload = async (): Promise<PosterRowEntry<T>[]> => {
    const seq = ++loadSeq;
    error = "";
    try {
      const fresh = await fetchEntries(count);
      // a newer load (count changed mid-flight) owns the row now
      if (seq === loadSeq) entries = fresh;
    } catch (err: any) {
      console.error(`Error loading ${title.toLowerCase()}:`, err);
      if (seq === loadSeq) error = err.message;
    } finally {
      if (seq === loadSeq) loading = false;
    }
    return entries;
  };

  // load on mount, then again whenever the row is expanded or resized; the
  // previous entries stay on screen while the new ones come in
  let mounted = $state(false);
  $effect(() => {
    count;
    if (mounted && !collapsed) reload();
  });

  onMount(() => {
    mounted = true;
    const timer = setInterval(() => {
      if (!collapsed) reload();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  });
</script>

<section class="bg-card rounded-lg border border-border p-5">
  <div class="flex items-center justify-between gap-3">
    <h2 class="text-xl font-semibold text-foreground">{title}</h2>
    <div class="flex items-center gap-2">
      {#if !collapsed}
        <Select.Root
          type="single"
          value={count.toString()}
          onValueChange={(v) => (count = parseInt(v))}
        >
          <Select.Trigger
            size="sm"
            class="w-20 bg-card text-card-foreground"
            aria-label="Titles to show"
          >
            {count}
          </Select.Trigger>
          <Select.Content class="bg-card">
            {#each COUNT_OPTIONS as option}
              <Select.Item
                value={option.toString()}
                label={option.toString()}
                class="text-card-foreground"
              >
                {option}
              </Select.Item>
            {/each}
          </Select.Content>
        </Select.Root>
      {/if}
      <button
        type="button"
        class="rounded-md p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground
          transition-colors cursor-pointer"
        aria-label={`${collapsed ? "Expand" : "Collapse"} ${title.toLowerCase()}`}
        aria-expanded={!collapsed}
        onclick={() => (collapsed = !collapsed)}
      >
        <ChevronDown
          class={`size-5 transition-transform ${collapsed ? "-rotate-90" : ""}`}
        />
      </button>
    </div>
  </div>

  {#if collapsed}
    <!-- collapsed: header only, nothing fetched -->
  {:else if loading}
    <p class="mt-4 text-muted-foreground text-center py-8">Loading...</p>
  {:else if error}
    <p class="mt-4 text-destructive text-center py-8">{error}</p>
  {:else if entries.length === 0}
    <p class="mt-4 text-muted-foreground text-center py-8">{emptyText}</p>
  {:else}
    <div class="mt-4 flex gap-3 overflow-x-auto pb-2">
      {#each entries as entry (entry.key)}
        <Tooltip.Root>
          <Tooltip.Trigger
            class="w-28 shrink-0 text-left cursor-pointer [&_*]:cursor-pointer"
            onclick={() => onSelect(entry)}
          >
            <PosterThumb
              mediaType={entry.mediaType}
              posterUrl={entry.posterUrl}
              posterSize="185"
              tailWindElSize="w-28"
              showMediaType
            />
            <p class="mt-1.5 text-sm font-medium text-foreground truncate">
              {entry.title}
            </p>
            <p class="text-xs text-muted-foreground truncate">
              {entry.subtitle}
            </p>
          </Tooltip.Trigger>
          <Tooltip.Content>
            {#each entry.tooltip as line}
              <p>{line}</p>
            {/each}
          </Tooltip.Content>
        </Tooltip.Root>
      {/each}
    </div>
  {/if}
</section>
