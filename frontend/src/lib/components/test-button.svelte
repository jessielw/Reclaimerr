<script lang="ts">
  import { Button } from "$lib/components/ui/button";
  import Check from "@lucide/svelte/icons/check";
  import X from "@lucide/svelte/icons/x";
  import TestTube from "@lucide/svelte/icons/test-tube";
  import TestTubeDiagonal from "@lucide/svelte/icons/test-tube-diagonal";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";

  type TestButtonStatus = "idle" | "loading" | "success" | "error";

  let {
    children = undefined,
    class: className = "",
    loading = false,
    status = undefined,
    ...restProps
  } = $props();

  let selfHovered = $state(false);
  const effectiveStatus = $derived<TestButtonStatus>(
    loading ? "loading" : (status ?? "idle"),
  );
</script>

<Button
  class={"text-primary-foreground cursor-pointer gap-2 " + " " + className}
  onmouseenter={() => (selfHovered = true)}
  onmouseleave={() => (selfHovered = false)}
  {...restProps}
>
  {#if effectiveStatus === "loading"}
    <Spinner class="h-[1.2rem] w-[1.2rem]" />
  {:else if effectiveStatus === "success"}
    <Check class="h-[1.2rem] w-[1.2rem] text-green-500" />
  {:else if effectiveStatus === "error"}
    <X class="h-[1.2rem] w-[1.2rem] text-destructive" />
  {:else if selfHovered}
    <TestTubeDiagonal class="h-[1.2rem] w-[1.2rem]" />
  {:else}
    <TestTube class="h-[1.2rem] w-[1.2rem]" />
  {/if}
  {@render children?.()}
</Button>
