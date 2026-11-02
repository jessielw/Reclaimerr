<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import type { DecisionContext, DecisionTrace } from "$lib/types/diagnostics";
  import {
    evidenceValue,
    observedValue,
    outcomeLabel,
    targetLabel,
    ruleLogic,
  } from "./evidence-format.js";
  let {
    decisions = null,
    traces = [],
    onlookup,
    onload,
    loading = false,
  }: {
    decisions?: DecisionContext | null;
    traces?: DecisionTrace[];
    onlookup?: (code: string) => void;
    onload?: () => void;
    loading?: boolean;
  } = $props();
</script>

<details class="rounded-md border p-3 text-sm space-y-3">
  <summary class="cursor-pointer font-medium">Why this candidate?</summary>
  {#if traces.length}
    <p class="text-muted-foreground">
      Recorded evaluations, newest first. Values are from the scan, with private
      text masked. A matched rule does not by itself prove an item was eligible
      for deletion.
    </p>
    {#each traces as trace (trace.id)}
      <details class="rounded border p-3 space-y-3">
        <summary class="cursor-pointer"
          ><strong>Rule #{trace.rule_id} · {outcomeLabel(trace.outcome)}</strong
          ><span class="block text-xs text-muted-foreground"
            >{targetLabel(trace.target)} · {new Date(
              trace.observed_at,
            ).toLocaleString()}{trace.candidate_id
              ? ` · Candidate #${trace.candidate_id}`
              : ""}</span
          ></summary
        >
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <caption class="sr-only">Observed rule conditions</caption><thead
              ><tr
                ><th class="p-2">Condition</th><th class="p-2">Expected</th><th
                  class="p-2">Observed</th
                ><th class="p-2">Result</th></tr
              ></thead
            ><tbody>
              {#each trace.conditions as condition}<tr
                  class="border-t align-top"
                  ><td class="p-2 min-w-32"
                    >{condition.field_label || condition.field}<span
                      class="block text-muted-foreground"
                      >{condition.operator_label ||
                        condition.operator?.replaceAll("_", " ") ||
                        "Unknown operator"}</span
                    ></td
                  ><td class="p-2 break-words max-w-52"
                    >{evidenceValue(condition.expected)}</td
                  ><td class="p-2 break-words max-w-52"
                    >{observedValue(condition)}</td
                  ><td class="p-2">{outcomeLabel(condition.outcome)}</td></tr
                >{/each}
            </tbody>
          </table>
        </div>
        {#if !trace.conditions.length}<p class="text-muted-foreground">
            Condition-level evidence is unavailable for this trace.
          </p>{/if}
        <p class="text-xs text-muted-foreground">
          Disabled and short-circuited conditions were not checked. Unknown
          inputs are not zero or a successful check.
        </p>
        <details>
          <summary class="cursor-pointer font-medium"
            >Captured rule logic (ALL / ANY groups)</summary
          >
          <pre
            class="mt-2 whitespace-pre-wrap break-words rounded bg-muted p-2 text-xs">{ruleLogic(
              trace.definition,
            )}</pre>
        </details>
        <div class="space-y-1">
          <p class="font-medium">
            Protection context after scan reconciliation
          </p>
          {#if trace.protection_checks.protections.length}<ul
              class="list-disc pl-5"
            >
              {#each trace.protection_checks.protections as protection}<li>
                  Protection #{protection.id}: {protection.permanent
                    ? "permanent"
                    : protection.expires_at
                      ? `expires ${new Date(protection.expires_at).toLocaleString()}`
                      : "expiry not recorded"}{protection.source_rule_id
                    ? ` · Rule #${protection.source_rule_id}`
                    : ""}
                </li>{/each}
            </ul>{:else}<p class="text-muted-foreground">
              No protection rows were captured here. This does not establish
              that every protection check passed.
            </p>{/if}
          {#if trace.protection_checks.truncated}<p
              class="text-amber-600 dark:text-amber-400"
            >
              Protection context was sampled.
            </p>{/if}
        </div>
        {#if trace.scan_context}
          <div class="space-y-1">
            <p class="font-medium">Scan prerequisites</p>
            <p>
              Favorites refresh: {trace.scan_context
                .favorites_refresh_available === undefined
                ? "not recorded"
                : trace.scan_context.favorites_refresh_available
                  ? "available"
                  : "unavailable"} · Seerr refresh: {trace.scan_context
                .seerr_refresh_available === undefined
                ? "not recorded"
                : trace.scan_context.seerr_refresh_available
                  ? "available"
                  : "unavailable"}
            </p>
            {#if trace.scan_context.playback_blocking}<p>
                Playback prerequisites blocked some evaluation.
              </p>{/if}
            {#if trace.scan_context.skipped_rule_ids?.length}<p>
                Skipped rules: {trace.scan_context.skipped_rule_ids
                  .map((id) => `#${id}`)
                  .join(", ")}
              </p>{/if}
            {#if trace.scan_context.playback_unavailable_targets}<p>
                {trace.scan_context.playback_unavailable_targets} targets had unavailable
                playback inputs.
              </p>{/if}
            {#if trace.scan_context.dropped_traces}<p>
                {trace.scan_context.dropped_traces} observations exceeded recording
                limits.
              </p>{/if}
          </div>
        {/if}
        <p class="text-xs break-all text-muted-foreground">
          Captured rule revision: {trace.rule_revision}. Current saved rules may
          differ.
        </p>
        <p class="text-xs break-all font-mono">
          scan-{trace.scan_id}{trace.job_id ? ` · job-${trace.job_id}` : ""}
        </p>
        {#if onlookup}<Button
            variant="outline"
            size="sm"
            onclick={() => onlookup?.(`scan-${trace.scan_id}`)}
            >Look up this scan</Button
          >{/if}
      </details>
    {/each}
  {:else}
    {#if onload}<Button
        variant="outline"
        disabled={loading}
        onclick={() => onload?.()}>Load recorded scan evidence</Button
      >{/if}
    <p class="text-muted-foreground">
      No historical evaluations are loaded here. Prepare a candidate support
      bundle to view recorded scans. Older scans and targets skipped before
      evaluation may have no trace.
    </p>
  {/if}
  {#if decisions?.candidates.length}
    <details class="space-y-2" open={!traces.length}>
      <summary class="cursor-pointer font-medium"
        >Current candidate's recorded match inputs</summary
      >
      <p class="text-muted-foreground">
        These saved candidate inputs do not establish the exact historical scan
        or the result of every condition.
      </p>
      {#each decisions.candidates as candidate}<div
          class="rounded border p-3 space-y-2"
        >
          <p class="font-medium">
            Candidate #{candidate.id} · {targetLabel(candidate.target)}
          </p>
          <p>
            Recorded rules: {candidate.matched_rule_ids
              .map((id) => `#${id}`)
              .join(", ") || "Unavailable"}
          </p>
          {#each candidate.conditions as condition}<p class="break-words">
              <strong>{condition.field_label || condition.field}</strong>
              {condition.operator?.replaceAll("_", " ") || ""} · expected {evidenceValue(
                condition.expected,
              )} · recorded {evidenceValue(condition.actual)}
            </p>{/each}
          {#if !candidate.conditions.length}<p class="text-muted-foreground">
              No condition inputs were saved on this candidate.
            </p>{/if}
        </div>{/each}
    </details>
  {/if}
  {#if decisions?.protections.length}<details class="space-y-2">
      <summary class="cursor-pointer font-medium"
        >Current saved protections</summary
      >
      <p class="text-muted-foreground">
        Current state, separate from protection context captured after a
        historical scan.
      </p>
      <ul class="list-disc pl-5">
        {#each decisions.protections as protection}<li>
            {targetLabel(protection.target)} · {protection.permanent
              ? "permanent"
              : protection.expires_at
                ? `expires ${new Date(protection.expires_at).toLocaleString()}`
                : "expiry not recorded"}
          </li>{/each}
      </ul>
    </details>{/if}
</details>
