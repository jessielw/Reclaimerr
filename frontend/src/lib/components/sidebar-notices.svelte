<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { auth } from "$lib/stores/auth";
  import { get_api } from "$lib/api";
  import { formatDateTimeToLocaleString } from "$lib/utils/date";
  import Bell from "@lucide/svelte/icons/bell";
  import BellDot from "@lucide/svelte/icons/bell-dot";
  import Download from "@lucide/svelte/icons/download";
  import * as Dialog from "$lib/components/ui/dialog/index.js";

  const minutesToCheckForUpdates = 5;

  let updateAvailable = $state(false);
  let latestVersion = $state<string | null>(null);
  let latestReleaseUrl = $state<string | null>(null);
  let lastCheckedAt = $state<string | null>(null);
  let noticesDialogOpen = $state(false);

  interface UpdateStatusResponse {
    update_available: boolean;
    latest_version: string | null;
    latest_release_url: string | null;
    last_checked_at: string | null;
  }

  const loadUpdateStatus = async () => {
    if (!$auth.isAuthenticated || $auth.user?.role !== "admin") {
      updateAvailable = false;
      latestVersion = null;
      latestReleaseUrl = null;
      lastCheckedAt = null;
      return;
    }
    try {
      const status = await get_api<UpdateStatusResponse>(
        "/api/info/update-status",
      );
      console.log("Update status:", status);
      updateAvailable = !!status.update_available;
      latestVersion = status.latest_version;
      latestReleaseUrl = status.latest_release_url;
      lastCheckedAt = status.last_checked_at;
    } catch {
      updateAvailable = false;
      latestVersion = null;
      latestReleaseUrl = null;
      lastCheckedAt = null;
    }
  };

  const openLatestRelease = () => {
    if (!latestReleaseUrl) return;
    window.open(latestReleaseUrl, "_blank", "noopener,noreferrer");
  };

  const formattedLastCheckedAt = $derived.by(() =>
    lastCheckedAt ? formatDateTimeToLocaleString(lastCheckedAt) : null,
  );

  let updateStatusInterval: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    loadUpdateStatus();
    updateStatusInterval = setInterval(
      loadUpdateStatus,
      minutesToCheckForUpdates * 60 * 1000,
    );
  });

  onDestroy(() => {
    if (!updateStatusInterval) return;
    clearInterval(updateStatusInterval);
    updateStatusInterval = null;
  });
</script>

{#if $auth.user?.role === "admin"}
  <button
    type="button"
    class="relative inline-flex items-center justify-center rounded-md p-1
      hover:bg-accent hover:text-accent-foreground cursor-pointer"
    aria-label="Open notices"
    title={updateAvailable
      ? latestVersion
        ? `Update available: ${latestVersion}`
        : "Update available"
      : "Notices"}
    onclick={() => (noticesDialogOpen = true)}
  >
    {#if updateAvailable}
      <BellDot class="size-4.5 text-green-500" />
    {:else}
      <Bell class="size-4.5 text-muted-foreground/35" />
    {/if}
  </button>

  <Dialog.Root bind:open={noticesDialogOpen}>
    <Dialog.Content class="sm:max-w-lg">
      <Dialog.Header>
        <Dialog.Title>Notices</Dialog.Title>
      </Dialog.Header>

      <!-- update notices -->
      <div class="space-y-2 py-2">
        {#if updateAvailable && latestReleaseUrl}
          <p class="text-sm text-foreground">
            A new release is available
            {#if latestVersion}
              (<span class="font-medium">{latestVersion}</span>)
            {/if}
          </p>
          <button
            type="button"
            class="inline-flex items-center gap-2 text-sm text-primary hover:underline cursor-pointer"
            onclick={openLatestRelease}
          >
            <Download class="size-4" />
            View release details
          </button>
        {:else}
          <p class="text-sm text-muted-foreground">No notices yet.</p>
        {/if}
        {#if formattedLastCheckedAt}
          <p class="text-xs text-muted-foreground">
            Last checked: {formattedLastCheckedAt}
          </p>
        {/if}
      </div>
    </Dialog.Content>
  </Dialog.Root>
{/if}
