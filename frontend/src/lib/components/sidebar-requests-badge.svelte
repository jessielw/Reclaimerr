<script lang="ts">
  import { uiIndicators } from "$lib/stores/ui-indicators";
</script>

{#if $uiIndicators.hasPendingRequests}
  <span
    class="ml-auto inline-flex size-2 rounded-full bg-amber-400 shrink-0"
    aria-label="Pending requests available"
    title="Pending requests available"
  ></span>
{/if}
