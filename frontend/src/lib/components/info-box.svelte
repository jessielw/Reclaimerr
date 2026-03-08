<script lang="ts">
  import InfoIcon from "@lucide/svelte/icons/info";

  interface Props {
    title?: string;
    children?: any;
  }
  let { title = "Info", children = undefined }: Props = $props();
</script>

<div
  class="flex flex-col border border-blue-300/90 dark:border-sky-500/80 overflow-hidden
  rounded-[0.095rem] shadow"
>
  <span
    class="flex items-center gap-2 bg-blue-300/90 dark:bg-sky-400/80 p-1 pl-3"
  >
    <InfoIcon class="size-4 text-gray-900" />
    <span class="text-gray-900 text-sm font-semibold">{title}</span>
  </span>
  {#if children}
    <div class="text-sm p-3 text-foreground/85">
      {@render children?.()}
    </div>
  {/if}
</div>
