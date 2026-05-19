<script lang="ts">
  import { auth } from "$lib/stores/auth";
  import { uiIndicators } from "$lib/stores/ui-indicators";
  import { formatDateTimeToLocaleString } from "$lib/utils/date";
  import Bell from "@lucide/svelte/icons/bell";
  import BellDot from "@lucide/svelte/icons/bell-dot";
  import Download from "@lucide/svelte/icons/download";
  import * as Dialog from "$lib/components/ui/dialog/index.js";

  let noticesDialogOpen = $state(false);

  const openLatestRelease = () => {
    if (!$uiIndicators.latestReleaseUrl) return;
    window.open(
      $uiIndicators.latestReleaseUrl,
      "_blank",
      "noopener,noreferrer",
    );
  };

  const formattedLastCheckedAt = $derived.by(() =>
    $uiIndicators.lastCheckedAt
      ? formatDateTimeToLocaleString($uiIndicators.lastCheckedAt)
      : null,
  );
</script>

{#if $auth.user?.role === "admin"}
  <button
    type="button"
    class="relative inline-flex items-center justify-center rounded-md p-1
      hover:bg-accent hover:text-accent-foreground cursor-pointer"
    aria-label="Open notices"
    title={$uiIndicators.updateAvailable
      ? $uiIndicators.latestVersion
        ? `Update available: ${$uiIndicators.latestVersion}`
        : "Update available"
      : "Notices"}
    onclick={() => (noticesDialogOpen = true)}
  >
    {#if $uiIndicators.updateAvailable}
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
        {#if $uiIndicators.updateAvailable && $uiIndicators.latestReleaseUrl}
          <p class="text-sm text-foreground">
            A new release is available
            {#if $uiIndicators.latestVersion}
              (<span class="font-medium">{$uiIndicators.latestVersion}</span>)
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
