<script lang="ts">
  import Badge from "$lib/components/ui/badge/badge.svelte";
  import type { ReclaimCandidateEntry } from "$lib/types/shared";
  import { detailReasons, ruleNames } from "$lib/utils/candidate-rules";

  interface Props {
    entry: ReclaimCandidateEntry;
  }

  let { entry }: Props = $props();
  const rules = $derived(ruleNames(entry));
  const reasons = $derived(detailReasons(entry));
</script>

{#if rules.length > 0 || reasons.length > 0}
  <div class="mt-2 space-y-2">
    {#if rules.length > 0}
      <div class="space-y-1">
        <div class="text-[11px] uppercase tracking-wide text-muted-foreground">
          Matched rules
        </div>
        <div class="flex flex-wrap gap-1.5">
          {#each rules as rule}
            <Badge
              class="border-primary break-all whitespace-normal"
              variant="secondary">{rule}</Badge
            >
          {/each}
        </div>
      </div>
    {/if}

    {#if reasons.length > 0}
      <div class="space-y-1">
        <div class="text-[11px] uppercase tracking-wide text-muted-foreground">
          Why matched
        </div>
        <ul class="space-y-1 text-xs leading-5 text-muted-foreground">
          {#each reasons as reason}
            <li class="wrap-break-word">{reason}</li>
          {/each}
        </ul>
      </div>
    {/if}
  </div>
{/if}
