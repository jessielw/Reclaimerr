<script lang="ts">
  interface Props {
    selector?: any;
    actions?: any;
    className?: string;
  }

  let {
    selector = undefined,
    actions = undefined,
    className = "",
  }: Props = $props();
</script>

<div
  class={`mb-5 flex flex-wrap items-center justify-between gap-3 rounded-md border 
    border-border bg-background/50 p-3 ${className}`}
>
  <div class="flex flex-wrap min-w-0 items-center gap-3">
    {@render selector?.()}
  </div>
  <div class="flex flex-wrap items-center gap-2">
    {@render actions?.()}
  </div>
</div>
