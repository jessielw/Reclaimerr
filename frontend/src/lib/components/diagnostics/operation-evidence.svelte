<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import type { TimelineEntry } from "$lib/types/diagnostics";
  import { outcomeLabel, targetLabel } from "./evidence-format.js";
  let {
    entries,
    onlookup,
  }: { entries: TimelineEntry[]; onlookup: (code: string) => void } = $props();
  const observations = $derived(entries.filter((entry) => entry.operation));
</script>

{#if observations.length}
  <details
    class="rounded-md border p-3 space-y-3 text-sm"
    open={observations.some((entry) => entry.operation?.outcome === "failed")}
  >
    <summary class="cursor-pointer font-medium"
      >Per-item operation outcomes ({observations.length} observations)</summary
    >
    <p class="text-muted-foreground">
      File steps and candidate outcomes are separate. A later successful
      fallback does not erase an earlier failure. Provider requests can cover
      multiple files and do not verify their filesystem state.
    </p>
    <ol class="space-y-3">
      {#each observations as entry}
        {@const detail = entry.operation!}
        <li class="rounded border p-3 space-y-1">
          <p class="font-medium">
            {outcomeLabel(detail.outcome)} · {detail.stage.replaceAll("_", " ")} ·
            {entry.event === "item_outcome"
              ? "candidate outcome"
              : detail.subject.replaceAll("_", " ")}
          </p>
          <p class="text-xs text-muted-foreground">
            {targetLabel(entry.target)} · Candidate #{entry.candidate_id} · {new Date(
              entry.occurred_at,
            ).toLocaleString()}
          </p>
          {#if detail.path}<p class="break-all">Source: {detail.path}</p>{/if}
          {#if detail.destination}<p class="break-all">
              Destination: {detail.destination}
            </p>{/if}
          {#if detail.provider}<p>
              Provider: {detail.provider.replaceAll("_", " ")}{detail.reference
                ? ` · ${detail.reference}`
                : ""}
            </p>{/if}
          {#if entry.error_category}<p>
              Reason: {entry.error_category.replaceAll("_", " ")}
            </p>{/if}
          {#if detail.shared_request}<p class="text-muted-foreground">
              Shared request associated with this candidate; not an individual
              file result.
            </p>{/if}
          {#if detail.outcome === "unknown"}<p class="text-muted-foreground">
              No explicit final outcome was recorded. Do not infer success from
              a completed batch.
            </p>{/if}
          {#if detail.dropped_observations}<p class="text-muted-foreground">
              Some steps exceeded the recording limit.
            </p>{/if}
          <Button
            variant="outline"
            size="sm"
            onclick={() => onlookup(`operation-${entry.correlation_id}`)}
            >Look up this operation</Button
          >
        </li>
      {/each}
    </ol>
  </details>
{/if}
