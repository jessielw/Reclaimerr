<script lang="ts">
  import { untrack, tick } from "svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { fetchAPI } from "$lib/api";
  import type {
    DiagnosticsTarget,
    SupportLookupResult,
    SupportMatch,
  } from "$lib/types/diagnostics";
  import { targetLabel } from "./evidence-format.js";
  let {
    open,
    request,
    busy = false,
    onprepare,
  }: {
    open: boolean;
    request?: { code: string; revision: number };
    busy?: boolean;
    onprepare: (match: SupportMatch, target: DiagnosticsTarget | null) => void;
  } = $props();
  let expanded = $state(false);
  let code = $state("");
  let loading = $state(false);
  let error = $state("");
  let result = $state<SupportLookupResult | null>(null);
  let searchedCode = $state("");
  let selected = $state<Record<string, string>>({});
  let controller: AbortController | undefined;
  let panel: HTMLDetailsElement | undefined;
  $effect(() => {
    void open;
    return () => {
      controller?.abort();
      loading = false;
    };
  });
  $effect(() => {
    if (request && open) {
      const incoming = request;
      untrack(() => {
        code = incoming.code;
        expanded = true;
        void search();
        void tick().then(() => panel?.scrollIntoView({ block: "nearest" }));
      });
    }
  });
  async function search() {
    controller?.abort();
    const current = new AbortController();
    controller = current;
    const submitted = code.trim();
    loading = true;
    error = "";
    const timer = setTimeout(() => {
      current.abort();
      loading = false;
      error = "Lookup timed out. Try again when the app is available.";
    }, 10000);
    try {
      const response = await fetchAPI("/api/diagnostics/lookup", {
        method: "POST",
        body: JSON.stringify({ code: submitted }),
        signal: current.signal,
      });
      if (!response.ok)
        throw new Error(
          response.status === 422
            ? "Enter job-123, scan-UUID, operation-UUID, or a correlation UUID."
            : response.status === 429
              ? "Diagnostics are busy. Try again shortly."
              : "Could not look up this code. Administrator access is required.",
        );
      const data: SupportLookupResult = await response.json();
      if (current.signal.aborted) return;
      result = data;
      searchedCode = submitted;
      selected = {};
    } catch (cause) {
      if (!current.signal.aborted)
        error = cause instanceof Error ? cause.message : "Lookup failed.";
    } finally {
      clearTimeout(timer);
      if (!current.signal.aborted) loading = false;
    }
  }
  function prepare(match: SupportMatch) {
    const choice = selected[match.code];
    const target = choice
      ? (match.targets.find((item) => `${item.scope}:${item.id}` === choice) ??
        null)
      : match.suggested.target;
    onprepare(match, target);
  }
</script>

<details
  bind:this={panel}
  bind:open={expanded}
  class="rounded-md border p-3 space-y-3 text-sm"
>
  <summary class="cursor-pointer font-medium"
    >Find evidence by support code</summary
  >
  <p class="text-muted-foreground">
    Paste a scan, operation or job code from the timeline or logs. Lookup reads
    stored history only. It does not rerun the operation.
  </p>
  <form
    class="flex flex-wrap items-end gap-2"
    onsubmit={(event) => {
      event.preventDefault();
      void search();
    }}
  >
    <label class="flex-1 min-w-40"
      >Support code<input
        class="mt-1 block w-full rounded-md border bg-background p-2 font-mono text-xs"
        bind:value={code}
        maxlength={80}
        placeholder="job-123 or scan-…"
        autocomplete="off"
        spellcheck="false"
        disabled={loading || busy}
      /></label
    >
    <Button
      variant="outline"
      type="submit"
      disabled={loading || busy || !code.trim()}
      >{loading ? "Searching…" : "Look up code"}</Button
    >
  </form>
  <div aria-live="polite" class="space-y-2">
    {#if error}<p role="alert" class="text-destructive">{error}</p>{/if}
    {#if result}
      {#if searchedCode !== code.trim()}<p class="text-muted-foreground">
          Code changed. Look it up before preparing evidence.
        </p>{/if}
      {#if result.state === "not_found"}<p>
          No stored match for this code. It may be expired, mistyped, or from
          before recording was introduced.
        </p>{/if}
      {#if result.state === "unavailable"}<p>
          History is unavailable. This does not mean the code has no match.
        </p>{/if}
      {#each result.matches as match}<div class="rounded border p-3 space-y-2">
          <p class="font-mono text-xs break-all">{match.code}</p>
          {#if match.occurred_at}<p class="text-muted-foreground">
              Recorded {new Date(match.occurred_at).toLocaleString()}
            </p>{/if}
          {#if match.targets.length > 1 || match.targets_truncated}<label
              >Evidence scope<select
                class="mt-1 block w-full rounded border bg-background p-2"
                bind:value={selected[match.code]}
                disabled={busy}
                ><option value="">All targets associated with this code</option
                >{#each match.targets as target}<option
                    value={`${target.scope}:${target.id}`}
                    >{targetLabel(target)}</option
                  >{/each}</select
              ></label
            >{:else if match.targets.length}<p>
              {targetLabel(match.targets[0])}
            </p>{:else}<p>
              No retained media association; the bundle will contain
              code-related history and system context.
            </p>{/if}
          {#if match.targets_truncated}<p class="text-muted-foreground">
              Target choices are limited to 100. The bundle also reports its
              collection limits.
            </p>{/if}
          <Button
            variant="outline"
            disabled={loading ||
              busy ||
              !!error ||
              searchedCode !== code.trim()}
            onclick={() => prepare(match)}>Prepare matching evidence</Button
          >
        </div>{/each}
      <details>
        <summary class="cursor-pointer text-muted-foreground"
          >Lookup coverage</summary
        >
        <ul class="list-disc pl-5">
          {#each result.limitations as note}<li>{note}</li>{/each}
        </ul>
      </details>
    {/if}
  </div>
</details>
