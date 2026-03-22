<script lang="ts">
  import { toggleMode, mode } from "mode-watcher";
  import { Button } from "$lib/components/ui/button";
  import SunIcon from "@lucide/svelte/icons/sun";
  import MoonIcon from "@lucide/svelte/icons/moon";

  let className: string = "";
  export { className as class };
</script>

<Button
  variant="ghost"
  size="icon"
  onclick={toggleMode}
  class={"text-foreground cursor-pointer " + " " + className}
  aria-label="Toggle theme"
>
  {#if mode.current === "dark"}
    <SunIcon class="h-[1.2rem] w-[1.2rem]" />
  {:else}
    <MoonIcon class="h-[1.2rem] w-[1.2rem]" />
  {/if}
</Button>
