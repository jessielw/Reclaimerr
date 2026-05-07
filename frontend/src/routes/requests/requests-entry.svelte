<script lang="ts">
  import DeleteRequests from "./deletion-requests.svelte";
  import ProtectionRequests from "./protection-requests.svelte";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import Shield from "@lucide/svelte/icons/shield";
  import { Button } from "$lib/components/ui/button/index.js";

  type RequestTab = "deletion" | "protection";
  let activeTab = $state<RequestTab>("protection");
</script>

<div class="p-2.5 md:p-8 pb-0 md:pb-0">
  <div class="mx-auto max-w-7xl">
    <div class="mb-4">
      <h1 class="text-3xl font-bold text-foreground mb-1">Requests</h1>
    </div>

    <div class="flex gap-1 rounded-lg border bg-muted/40 p-1 w-fit">
      <!-- protection tab -->
      <Button
        onclick={() => (activeTab = "protection")}
        class="cursor-pointer
          {activeTab === 'protection'
          ? 'bg-primary text-background dark:text-foreground'
          : 'text-foreground bg-transparent'}"
      >
        <Shield class="size-4" />
        Protection Requests
      </Button>

      <!-- delete tab -->
      <Button
        onclick={() => (activeTab = "deletion")}
        class="cursor-pointer
          {activeTab === 'deletion'
          ? 'bg-primary text-background dark:text-foreground'
          : 'text-foreground bg-transparent'}"
      >
        <Trash2 class="size-4" />
        Deletion Requests
      </Button>
    </div>
  </div>
</div>

{#if activeTab === "deletion"}
  <DeleteRequests />
{:else}
  <ProtectionRequests />
{/if}
