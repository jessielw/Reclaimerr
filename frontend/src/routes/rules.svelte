<script lang="ts">
  import Filter from "@lucide/svelte/icons/filter";
  import Rules from "$lib/components/settings/rules/rules.svelte";
  import { auth } from "$lib/stores/auth";
</script>

<div class="p-2.5 md:p-8 max-w-7xl mx-auto space-y-4">
  {#if $auth.user?.role === "admin"}
    <Rules svgIcon={Filter} />
  {:else}
    <div
      class="bg-card rounded-lg border border-border p-8 text-center text-muted-foreground"
    >
      You do not have access to this page.
    </div>
  {/if}
</div>
