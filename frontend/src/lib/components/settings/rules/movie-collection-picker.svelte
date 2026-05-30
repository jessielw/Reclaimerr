<script lang="ts">
  import { untrack } from "svelte";
  import { get_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import type {
    MovieCollectionLookup,
    PaginatedMovieCollectionsResponse,
  } from "$lib/types/shared";

  interface Props {
    open?: boolean;
    initialSelectedNames?: string[];
    allowMultiple?: boolean;
    onApply: (names: string[]) => void;
  }

  let {
    open = $bindable(false),
    initialSelectedNames = [],
    allowMultiple = true,
    onApply,
  }: Props = $props();

  const perPage = 50;
  let query = $state("");
  let loading = $state(false);
  let loadingMore = $state(false);
  let error = $state<string | null>(null);
  let items = $state<MovieCollectionLookup[]>([]);
  let page = $state(1);
  let totalPages = $state(0);
  let selectedNames = $state<string[]>([]);
  let searchDebounce: ReturnType<typeof setTimeout> | null = null;
  let wasOpen = $state(false);

  const hasMore = $derived(page < totalPages);

  const loadCollections = async (
    nextPage: number,
    opts: { append?: boolean; search?: string } = {},
  ) => {
    const append = opts.append ?? false;
    const needle = (opts.search ?? "").trim();
    if (!open) return;
    if (append) loadingMore = true;
    else loading = true;
    error = null;
    try {
      const params = new URLSearchParams();
      params.set("page", String(nextPage));
      params.set("per_page", String(perPage));
      if (needle) params.set("q", needle);

      const resp = await get_api<PaginatedMovieCollectionsResponse>(
        `/api/rules/movie-collections?${params.toString()}`,
      );
      page = resp.page;
      totalPages = resp.total_pages;
      if (append) items = [...items, ...resp.items];
      else items = resp.items;
    } catch (e: any) {
      error = e.message ?? "Failed to load movie collections.";
      if (!append) items = [];
    } finally {
      loading = false;
      loadingMore = false;
    }
  };

  const isSelected = (name: string) => selectedNames.includes(name);

  const toggleSelected = (name: string) => {
    if (!allowMultiple) {
      selectedNames = selectedNames[0] === name ? [] : [name];
      return;
    }
    if (selectedNames.includes(name)) {
      selectedNames = selectedNames.filter((item) => item !== name);
      return;
    }
    selectedNames = [...selectedNames, name];
  };

  const applySelection = () => {
    const unique = [
      ...new Set(selectedNames.map((v) => v.trim()).filter(Boolean)),
    ];
    const names = allowMultiple ? unique : unique.slice(0, 1);
    onApply(names);
    open = false;
  };

  const handleQueryInput = (value: string) => {
    query = value;
    if (searchDebounce) clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      void loadCollections(1, { search: value });
    }, 250);
  };

  const loadMore = () => {
    if (!hasMore || loadingMore || loading) return;
    void loadCollections(page + 1, { append: true, search: query });
  };

  $effect(() => {
    if (open && !wasOpen) {
      selectedNames = [
        ...new Set(
          untrack(() => initialSelectedNames)
            .map((item) => item.trim())
            .filter(Boolean),
        ),
      ];
      query = "";
      page = 1;
      totalPages = 0;
      items = [];
      void untrack(() => loadCollections(1, { search: "" }));
    }
    if (!open && searchDebounce) {
      clearTimeout(searchDebounce);
      searchDebounce = null;
    }
    wasOpen = open;
  });
</script>

<Dialog.Root bind:open>
  <Dialog.Content
    class="sm:max-w-2xl h-[min(90vh,44rem)] max-h-[90vh] p-0 flex flex-col overflow-hidden border-ring border-2
      text-foreground"
    onInteractOutside={(e) => {
      e.preventDefault();
    }}
  >
    <Dialog.Header class="px-6 pt-5 pb-3 shrink-0 border-b border-border">
      <Dialog.Title>Select Movie Collections</Dialog.Title>
      <Dialog.Description>
        Search local TMDB movie collections and apply them to this rule
        condition.
      </Dialog.Description>
    </Dialog.Header>

    <div class="flex-1 overflow-y-auto px-6 py-4 space-y-4 min-h-0">
      <Input
        type="text"
        placeholder="Search collections..."
        value={query}
        oninput={(e) => handleQueryInput(e.currentTarget.value)}
      />

      <div class="rounded-md border border-border overflow-y-auto min-h-0">
        {#if loading}
          <p class="p-3 text-sm text-muted-foreground">
            Loading collections...
          </p>
        {:else if error}
          <p class="p-3 text-sm text-destructive">{error}</p>
        {:else if items.length === 0}
          <p class="p-3 text-sm text-muted-foreground">No collections found.</p>
        {:else}
          <ul class="divide-y divide-border">
            {#each items as item (`${item.name}-${item.movie_count}`)}
              <li class="flex items-center gap-3 px-3 py-2">
                <input
                  type={allowMultiple ? "checkbox" : "radio"}
                  class="size-4 cursor-pointer"
                  checked={isSelected(item.name)}
                  oninput={() => toggleSelected(item.name)}
                />
                <button
                  type="button"
                  class="flex-1 text-left"
                  onclick={() => toggleSelected(item.name)}
                >
                  <p class="text-sm">{item.name}</p>
                  <p class="text-xs text-muted-foreground">
                    {item.movie_count} movie{item.movie_count === 1 ? "" : "s"}
                  </p>
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </div>

      {#if hasMore}
        <div class="flex justify-center">
          <Button
            type="button"
            variant="secondary"
            class="cursor-pointer"
            disabled={loadingMore}
            onclick={loadMore}
          >
            {loadingMore ? "Loading..." : "Load more"}
          </Button>
        </div>
      {/if}
    </div>

    <Dialog.Footer class="px-6 py-4 shrink-0 border-t border-border mt-auto">
      <Button
        variant="secondary"
        class="cursor-pointer"
        onclick={() => (open = false)}>Cancel</Button
      >
      <Button class="cursor-pointer" onclick={applySelection}>Apply</Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
