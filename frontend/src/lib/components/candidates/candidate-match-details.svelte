<script lang="ts">
  import Badge from "$lib/components/ui/badge/badge.svelte";
  import type { ReclaimCandidateEntry } from "$lib/types/shared";
  import { detailReasons, ruleDetails } from "$lib/utils/candidate-rules";

  interface Props {
    entry: ReclaimCandidateEntry;
  }

  let { entry }: Props = $props();
  const rules = $derived(ruleDetails(entry));
  const reasons = $derived(detailReasons(entry));
</script>

{#if rules.length > 0 || reasons.length > 0}
  <div class="mt-2 space-y-2">
    {#if rules.length > 0}
      <div class="space-y-1">
        <div class="text-[11px] uppercase tracking-wide text-muted-foreground">
          Matched rules
        </div>
        <div class="space-y-2">
          {#each rules as rule}
            <div class="min-w-0 space-y-1">
              <Badge
                class="border-primary break-all whitespace-normal"
                variant="secondary">{rule.name}</Badge
              >
              {#if rule.description}
                <p
                  class="mt-1 whitespace-pre-line text-xs text-foreground"
                >
                  {rule.description}
                </p>
              {/if}
            </div>
          {/each}
        </div>
      </div>
    {/if}

    {#if reasons.length > 0}
      <div class="space-y-1">
        <div class="text-[11px] uppercase tracking-wide text-muted-foreground">
          Why matched
        </div>
        <ul class="space-y-1 text-xs leading-5 text-foreground">
          {#each reasons as reason}
            <li class="wrap-break-word">{reason}</li>
          {/each}
        </ul>
      </div>
    {/if}
  </div>
{/if}
