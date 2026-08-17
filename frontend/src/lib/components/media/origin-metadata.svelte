<script lang="ts">
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import type { ArrRef, SeerrRequester } from "$lib/types/shared";

  interface Props {
    arrRefs?: ArrRef[];
    arrTags?: string[];
    seerrUrl?: string | null;
    seerrRequesters?: SeerrRequester[];
    compact?: boolean;
    class?: string;
  }

  let {
    arrRefs = [],
    arrTags = [],
    seerrUrl = null,
    seerrRequesters = [],
    compact = false,
    class: className = "",
  }: Props = $props();

  const hasContent = $derived(
    arrRefs.length > 0 ||
      arrTags.length > 0 ||
      !!seerrUrl ||
      seerrRequesters.length > 0,
  );
</script>

{#if hasContent}
  <section
    class={`${
      compact
        ? "space-y-1.5 text-xs"
        : "rounded border border-border/70 bg-muted/20 p-3 text-sm"
    } ${className}`}
  >
    {#if !compact}
      <h4 class="mb-1.5 text-xs uppercase tracking-wide text-muted-foreground">
        Source Information
      </h4>
    {/if}

    <div class="space-y-1.5">
      {#if arrRefs.length > 0 || seerrUrl}
        <div class="flex flex-wrap items-center gap-1.5">
          <span class="text-muted-foreground">Open in:</span>
          {#each arrRefs as ref (`${ref.service_config_id}-${ref.arr_id}`)}
            {@const label = ref.service_name ?? ref.service_type}
            {#if ref.item_url}
              <a
                href={ref.item_url}
                target="_blank"
                rel="noopener noreferrer"
                class="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-0.5 font-medium text-foreground hover:border-primary hover:text-primary"
              >
                {label}
                <ExternalLink class="size-3" />
              </a>
            {:else}
              <span
                class="inline-flex items-center rounded-md border border-border bg-card px-2 py-0.5 font-medium text-muted-foreground"
                title="Run Sync Media to populate this deep link"
              >
                {label}
              </span>
            {/if}
          {/each}
          {#if seerrUrl}
            <a
              href={seerrUrl}
              target="_blank"
              rel="noopener noreferrer"
              class="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-0.5 font-medium text-foreground hover:border-primary hover:text-primary"
            >
              Seerr
              <ExternalLink class="size-3" />
            </a>
          {/if}
        </div>
      {/if}

      {#if seerrRequesters.length > 0}
        <div class="flex flex-wrap items-center gap-1.5">
          <span class="text-muted-foreground">Requested by:</span>
          {#each seerrRequesters as requester (requester.user_id)}
            <Badge
              variant="secondary"
              title={requester.username &&
              requester.username !== requester.display_name
                ? `@${requester.username}`
                : undefined}
            >
              {requester.display_name}
            </Badge>
          {/each}
        </div>
      {/if}

      {#if arrTags.length > 0}
        <div class="flex flex-wrap items-center gap-1.5">
          <span class="text-muted-foreground">Arr tags:</span>
          {#each arrTags as tag (tag)}
            <Badge variant="outline">{tag}</Badge>
          {/each}
        </div>
      {/if}
    </div>
  </section>
{/if}
