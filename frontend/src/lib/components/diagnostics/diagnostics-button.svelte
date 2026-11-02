<script lang="ts">
  import Bug from "@lucide/svelte/icons/bug";
  import { Button } from "$lib/components/ui/button/index.js";
  import { auth } from "$lib/stores/auth";
  import DiagnosticsDialog from "./diagnostics-dialog.svelte";
  import type { DiagnosticsTarget } from "$lib/types/diagnostics";

  let {
    target,
    compact = false,
    label = "Debug info",
  }: {
    target?: DiagnosticsTarget;
    compact?: boolean;
    label?: string;
  } = $props();
  let open = $state(false);
</script>

{#if $auth.user?.role === "admin"}
  <Button
    variant="outline"
    size={compact ? "icon" : "default"}
    class={compact ? "rounded-full size-7" : ""}
    aria-label={label}
    title={label}
    onclick={() => (open = true)}
  >
    <Bug class="size-4 shrink-0" />
    {#if !compact}{label}{/if}
  </Button>
  {#if open}
    <DiagnosticsDialog bind:open {target} />
  {/if}
{/if}
