<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api, delete_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import Check from "@lucide/svelte/icons/check";
  import X from "@lucide/svelte/icons/x";
  import { toast } from "svelte-sonner";

  interface RestrictedDestination {
    id: number;
    username: string;
    name: string | null;
    state: "pending" | "approved";
    service: string;
    host: string;
    redacted_url: string;
    internal_host: boolean;
    fingerprint: string;
  }

  let loading = $state(true);
  let busyId = $state<number | null>(null);
  let destinations = $state<RestrictedDestination[]>([]);

  async function load() {
    try {
      destinations = await get_api<RestrictedDestination[]>(
        "/api/settings/notifications/restricted-destinations",
      );
    } catch (err: any) {
      toast.error(`Failed to load destination approvals: ${err.message}`);
    } finally {
      loading = false;
    }
  }

  async function approve(destination: RestrictedDestination) {
    try {
      busyId = destination.id;
      // the fingerprint pins the approval to the URL shown on screen
      await post_api(
        `/api/settings/notifications/${destination.id}/approval`,
        { fingerprint: destination.fingerprint },
      );
      toast.success("Destination approved");
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      busyId = null;
      await load();
    }
  }

  async function revoke(destination: RestrictedDestination) {
    try {
      busyId = destination.id;
      await delete_api(`/api/settings/notifications/${destination.id}/approval`);
      toast.success("Approval revoked");
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      busyId = null;
      await load();
    }
  }

  onMount(load);
</script>

{#if loading}
  <div class="flex justify-center p-4">
    <Spinner class="w-6 h-6 text-primary" />
  </div>
{:else if destinations.length > 0}
  <div class="border border-border rounded-lg bg-card p-4 mb-4 space-y-3">
    <div>
      <h3 class="font-semibold text-foreground">Destination approvals</h3>
      <p class="text-sm text-muted-foreground">
        Users can use Discord, Telegram, Slack, Pushover, Pushbullet and ntfy.sh
        freely. Any other destination makes this server send requests to a host
        the user chose, so it stays off until you approve that exact URL.
        Editing the URL withdraws the approval.
      </p>
    </div>

    {#each destinations as destination (destination.id)}
      <div
        class="flex flex-col md:flex-row md:items-center gap-3 justify-between
          border border-border rounded-md p-3"
      >
        <div class="min-w-0 space-y-1">
          <div class="flex flex-wrap items-center gap-2">
            <span class="font-medium text-foreground">
              {destination.name || destination.service}
            </span>
            <span class="text-sm text-muted-foreground">
              by {destination.username}
            </span>
            {#if destination.state === "approved"}
              <Badge variant="secondary">Approved</Badge>
            {:else}
              <Badge variant="outline">Pending</Badge>
            {/if}
            {#if destination.internal_host}
              <Badge variant="destructive">Internal address</Badge>
            {/if}
          </div>
          <p class="text-sm text-muted-foreground break-all">
            {destination.service} &rarr; {destination.host || "unknown host"}
          </p>
          <p class="text-xs font-mono text-muted-foreground break-all">
            {destination.redacted_url}
          </p>
        </div>
        <div class="flex gap-2 shrink-0">
          {#if destination.state === "pending"}
            <Button
              size="sm"
              class="gap-1"
              disabled={busyId === destination.id}
              onclick={() => approve(destination)}
            >
              <Check /> Approve
            </Button>
          {:else}
            <Button
              size="sm"
              variant="outline"
              class="gap-1"
              disabled={busyId === destination.id}
              onclick={() => revoke(destination)}
            >
              <X /> Revoke
            </Button>
          {/if}
        </div>
      </div>
    {/each}
  </div>
{/if}
