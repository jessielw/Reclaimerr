<script lang="ts">
  import { Button } from "$lib/components/ui/button";
  import TestTube from "@lucide/svelte/icons/test-tube";
  import TestTubeDiagonal from "@lucide/svelte/icons/test-tube-diagonal";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";

  let {
    children = undefined,
    class: className = "",
    loading = false,
    ...restProps
  } = $props();

  let selfHovered = $state(false);
</script>

<Button
  class={"text-primary-foreground cursor-pointer gap-2 " + " " + className}
  onmouseenter={() => (selfHovered = true)}
  onmouseleave={() => (selfHovered = false)}
  {...restProps}
>
  {#if loading}
    <Spinner class="h-[1.2rem] w-[1.2rem]" />
  {:else if selfHovered}
    <TestTubeDiagonal class="h-[1.2rem] w-[1.2rem]" />
  {:else}
    <TestTube class="h-[1.2rem] w-[1.2rem]" />
  {/if}
  {@render children?.()}
</Button>
