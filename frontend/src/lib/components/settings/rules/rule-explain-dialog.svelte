<script lang="ts">
  import { get_api, post_api } from "$lib/api";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import Notice from "$lib/components/notice.svelte";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import RuleExplainNode from "./rule-explain-node.svelte";
  import type {
    RuleExplainSnapshot,
    RuleExplainTarget,
    RuleExplainTargets,
    RuleExplanation,
  } from "$lib/types/rule-explain";

  let {
    open = $bindable(false),
    snapshot,
  }: { open: boolean; snapshot: RuleExplainSnapshot } = $props();
  let search = $state("");
  let targets = $state<RuleExplainTargets | null>(null);
  let selected = $state<RuleExplainTarget | null>(null);
  let result = $state<RuleExplanation | null>(null);
  let loading = $state(false);
  let checking = $state(false);
  let error = $state("");
  let searchError = $state("");
  let requestId = 0;
  let checkId = 0;

  const loadTargets = async (page = 1) => {
    const id = ++requestId;
    loading = true;
    searchError = "";
    const params = new URLSearchParams({
      target_scope: snapshot.target_scope,
      search,
      page: String(page),
    });
    try {
      const response = await get_api<RuleExplainTargets>(
        `/api/rules/explain/targets?${params}`,
      );
      if (id === requestId) targets = response;
    } catch (cause) {
      if (id === requestId) {
        targets = null;
        searchError =
          cause instanceof Error ? cause.message : "Could not load media.";
      }
    } finally {
      if (id === requestId) loading = false;
    }
  };

  $effect(() => {
    // Debounce typing; stale responses cannot replace a newer search.
    const query = search;
    if (!open) return;
    ++requestId;
    const timer = setTimeout(() => {
      void query;
      void loadTargets();
    }, 250);
    return () => clearTimeout(timer);
  });

  const choose = (target: RuleExplainTarget) => {
    ++checkId;
    checking = false;
    selected = target;
    result = null;
    error = "";
  };

  const check = async () => {
    if (!selected) return;
    const id = ++checkId;
    checking = true;
    error = "";
    result = null;
    try {
      const response = await post_api<RuleExplanation>("/api/rules/explain", {
        ...snapshot,
        target_id: selected.id,
      });
      if (id === checkId) result = response;
    } catch (cause) {
      if (id === checkId)
        error =
          cause instanceof Error ? cause.message : "Could not check this item.";
    } finally {
      if (id === checkId) checking = false;
    }
  };
</script>

<Dialog.Root bind:open>
  <Dialog.Content
    class="sm:max-w-3xl max-h-[90dvh] flex flex-col overflow-hidden bg-card text-card-foreground"
  >
    <Dialog.Header class="shrink-0">
      <Dialog.Title>Why doesn’t this match?</Dialog.Title>
      <Dialog.Description
        >Check one item against your current rule, including unsaved edits. This
        does not save the rule, create candidates or protections, or take
        actions.</Dialog.Description
      >
    </Dialog.Header>
    <div
      class="h-[55dvh] min-h-0 space-y-4 overflow-y-auto overflow-x-hidden pr-2"
    >
      <div class="space-y-2">
        <label for="rule-explain-search" class="text-sm font-medium"
          >Find an item</label
        >
        <Input
          id="rule-explain-search"
          bind:value={search}
          placeholder="Search by title or file path…"
        />
        <p class="text-xs text-muted-foreground">
          Includes items excluded from Preview. Scope: {snapshot.target_scope.replaceAll(
            "_",
            " ",
          )}.
        </p>
      </div>
      {#if searchError}
        <Notice type="error" title="Could not load items">{searchError}</Notice>
      {/if}
      <div
        class="max-h-52 overflow-y-auto rounded-md border"
        aria-busy={loading}
      >
        {#if loading}
          <div class="flex items-center gap-2 p-3 text-sm">
            <Spinner class="size-4" />Searching…
          </div>
        {:else if targets?.items.length}
          {#each targets.items as target (target.id)}
            <button
              type="button"
              class="w-full border-b last:border-b-0 p-3 text-left hover:bg-muted"
              class:bg-muted={selected?.id === target.id}
              aria-pressed={selected?.id === target.id}
              onclick={() => choose(target)}
            >
              <span class="block text-sm font-medium break-words"
                >{target.label}</span
              >
              {#if target.path}<span
                  class="block text-xs text-muted-foreground break-all"
                  >{target.path}</span
                >{/if}
            </button>
          {/each}
        {:else}
          <p class="p-3 text-sm text-muted-foreground">
            No indexed items found. Try another title or sync your library.
          </p>
        {/if}
      </div>
      {#if targets && targets.total > targets.per_page}
        <div class="flex items-center justify-between gap-2 text-sm">
          <Button
            variant="outline"
            size="sm"
            disabled={loading || targets.page <= 1}
            onclick={() => void loadTargets((targets?.page ?? 1) - 1)}
            >Previous</Button
          >
          <span
            >Page {targets.page} of {Math.ceil(
              targets.total / targets.per_page,
            )}</span
          >
          <Button
            variant="outline"
            size="sm"
            disabled={loading ||
              targets.page * targets.per_page >= targets.total}
            onclick={() => void loadTargets((targets?.page ?? 1) + 1)}
            >Next</Button
          >
        </div>
      {/if}
      {#if selected}
        <div class="space-y-2">
          <p class="text-sm break-words">
            <strong>Selected:</strong>
            {selected.label}
          </p>
          <Button disabled={checking} onclick={() => void check()}
            >{#if checking}<Spinner class="size-4" />Checking…{:else}Check this
              item{/if}</Button
          >
        </div>
      {/if}
      {#if error}<Notice type="error" title="Check failed">{error}</Notice>{/if}
      {#if result}
        <div class="space-y-3" aria-live="polite">
          <Notice
            type="info"
            title={result.eligible
              ? "This item matches"
              : result.exclusions.length
                ? "This item is excluded"
                : result.condition_state === "unknown"
                  ? "Required data is unavailable"
                  : "The conditions do not match"}
          >
            {#if result.eligible}This item qualifies for the selected rule
              outcome.{:else if result.condition_state === "pass"}The conditions
              pass. Eligibility checks explain why this item is absent from
              Preview.{:else}Review the condition results below. Unknown means
              data is unavailable; disabled conditions are ignored.{/if}
          </Notice>
          {#if result.exclusions.length}
            <div class="rounded-md border p-3">
              <h3 class="text-sm font-semibold">Eligibility checks</h3>
              <ul class="list-disc pl-5 text-sm space-y-1">
                {#each result.exclusions as exclusion}<li>
                    {exclusion}
                  </li>{/each}
              </ul>
            </div>
          {/if}
          {#each result.warnings as warning}<Notice
              type="warning"
              title="Source data warning">{warning}</Notice
            >{/each}
          <p class="text-xs text-muted-foreground">
            All enabled conditions are checked, including branches normally
            skipped after a failure. Results use the same data sources and
            comparisons as Preview. Long displayed values may be shortened.
          </p>
          <RuleExplainNode node={result.root} />
        </div>
      {/if}
    </div>
  </Dialog.Content>
</Dialog.Root>
