<script lang="ts">
  import type { Component } from "svelte";

  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();
</script>

<div>
  <div>
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {console.log(svgIcon)}
      {#if svgIcon}
        {@const Icon = svgIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      <span class="align-middle">About</span>
    </h2>
    <!-- <p class="text-sm text-muted-foreground mt-1">
      About Reclaimerr, links to docs, support, etc.
    </p> -->
  </div>
  Placeholder for about page. This will include info about the project, links to docs,
  support, etc.
</div>
