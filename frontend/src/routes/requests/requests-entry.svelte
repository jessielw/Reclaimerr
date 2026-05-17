<script lang="ts">
  import DeleteRequests from "./deletion-requests.svelte";
  import ProtectionRequests from "./protection-requests.svelte";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import Shield from "@lucide/svelte/icons/shield";
  import { Button } from "$lib/components/ui/button/index.js";
  import { createFilterState } from "$lib/utils/pagination";

  type RequestTab = "deletion" | "protection";
  const _activeTabStore = createFilterState<RequestTab>(
    "requests_active_tab",
    "protection",
  );
  let activeTab = $state<RequestTab>(
    _activeTabStore.getInitial() === "deletion" ? "deletion" : "protection",
  );

  $effect(() => _activeTabStore.save(activeTab));
</script>

<div class="p-2.5 md:p-8 pb-0 md:pb-0">
  <div class="max-w-7xl mx-auto space-y-4">
    <div>
      <h1 class="text-3xl font-bold text-foreground">Requests</h1>
      <p class="text-muted-foreground">
        Manage media item {activeTab} requests
      </p>
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
