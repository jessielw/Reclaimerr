<script lang="ts">
  import type { RuleExplainNode } from "$lib/types/rule-explain";
  import { formatFileSize } from "$lib/utils/formatters";

  let { node }: { node: RuleExplainNode } = $props();
  const labels = {
    pass: "Pass",
    fail: "Fail",
    unknown: "Unknown",
    disabled: "Disabled",
  };
  const stateClasses = {
    pass: "bg-green-500/15 text-green-600 dark:text-green-400",
    fail: "bg-destructive/15 text-destructive",
    unknown: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
    disabled: "bg-muted text-muted-foreground",
  };
  const valueLabel = (value: unknown, field: string | null = null): string => {
    if (value === null || value === undefined) return "No value";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (
      typeof value === "number" &&
      (field === "media.size" || field === "disk.free_bytes")
    )
      return `${formatFileSize(value)} (${value.toLocaleString()} bytes)`;
    if (Array.isArray(value))
      return value.length
        ? value.map((item) => valueLabel(item, field)).join(", ")
        : "Empty list";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  };
</script>

{#snippet tree(current: RuleExplainNode)}
  <div
    class="rounded-md border p-3 space-y-2"
    class:opacity-60={current.state === "disabled"}
  >
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0 text-sm font-medium break-words">
        {#if current.type === "group"}
          {current.op === "or" ? "ANY condition (OR)" : "ALL conditions (AND)"}
        {:else}
          {current.label || current.field}
          <span class="text-muted-foreground"
            >{current.operator_label || current.operator}</span
          >
        {/if}
      </div>
      <span
        class="shrink-0 text-xs font-semibold rounded px-2 py-1 {stateClasses[
          current.state
        ]}">{labels[current.state]}</span
      >
    </div>
    {#if current.type === "group"}
      <div class="space-y-2 border-l pl-3">
        {#each current.children as child}
          {@render tree(child)}
        {/each}
      </div>
    {:else}
      <dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt class="text-muted-foreground">Required</dt>
        <dd class="min-w-0 break-words whitespace-pre-wrap">
          {current.expected === null
            ? current.operator_label
            : valueLabel(current.expected, current.field)}
        </dd>
        <dt class="text-muted-foreground">Actual</dt>
        <dd class="min-w-0 break-words whitespace-pre-wrap">
          {current.state === "disabled"
            ? "Not evaluated"
            : current.state === "unknown"
              ? "Data unavailable"
              : valueLabel(current.actual, current.field)}
        </dd>
      </dl>
    {/if}
  </div>
{/snippet}

{@render tree(node)}
