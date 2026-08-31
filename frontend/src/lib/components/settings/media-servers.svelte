<script lang="ts">
  import { onMount } from "svelte";
  import { delete_api, get_api, post_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import JellyfinSVG from "$lib/components/svgs/jellyfin-svg.svelte";
  import EmbySVG from "$lib/components/svgs/emby-svg.svelte";
  import PlexSVG from "$lib/components/svgs/plex-svg.svelte";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import X from "@lucide/svelte/icons/x";
  import AlertTriangle from "@lucide/svelte/icons/triangle-alert";
  import Plus from "@lucide/svelte/icons/plus";
  import Server from "@lucide/svelte/icons/server";
  import { toast } from "svelte-sonner";
  import { SettingsTab } from "$lib/types/shared";
  import * as Select from "$lib/components/ui/select/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import Notice from "$lib/components/notice.svelte";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import { MEDIA_SERVERS as SERVERS } from "$lib/types/shared";
  import MediaServerCard from "./media-servers/media-server-card.svelte";
  import MediaServerDialog from "./media-servers/media-server-dialog.svelte";
  import type {
    MediaServerConfig,
    MediaServerState,
    SaveServiceResponse,
    ServerKey,
  } from "./media-servers/types";

  type MediaServerSyncResponse = SyncMediaRunResponse & {
    task: string;
    scope: "main" | "linked";
  };

  type AffectedRuleSummary = {
    id: number;
    name: string;
    removed_library_ids: string[];
    remaining_library_ids: string[];
  };

  type SyncMediaRunResponse = {
    status: string;
    message: string;
    job_id: number | null;
    already_active: boolean;
  };

  type BackgroundJobPollResponse = {
    id: number;
    status: string;
    error_message: string | null;
    payload?: {
      result?: {
        library_sync?: {
          affected_rules: AffectedRuleSummary[];
        };
      };
    };
  };

  const SERVER_ICONS: Record<ServerKey, any> = {
    jellyfin: JellyfinSVG,
    emby: EmbySVG,
    plex: PlexSVG,
  };

  const SERVER_LABELS: Record<ServerKey, string> = {
    jellyfin: "Jellyfin",
    emby: "Emby",
    plex: "Plex",
  };

  const SERVER_URL_PLACEHOLDERS: Record<ServerKey, string> = {
    jellyfin: "e.g. http://localhost:8096",
    emby: "e.g. http://localhost:8096",
    plex: "e.g. http://localhost:32400",
  };

  const apiKeyLabelFor = (serverKey: ServerKey): string =>
    serverKey === SettingsTab.Plex ? "Token" : "API Key";

  let servers = $state<MediaServerState[]>([]);
  let loading = $state(false);

  // the type picked in the "add a server" bar
  let addServerKey = $state<ServerKey>(SERVERS[0]);

  // the server currently open in the add/edit dialog. `existing` is null when
  // adding, so a draft never enters `servers` - a row appears in the list only
  // once the API has accepted it.
  let dialogOpen = $state(false);
  let dialogTarget = $state<{
    serverKey: ServerKey;
    existing: MediaServerState | null;
  } | null>(null);

  let deleteTarget = $state<MediaServerState | null>(null);
  let deletingServer = $state(false);
  let promoteTarget = $state<MediaServerState | null>(null);
  let promoting = $state(false);

  let syncingMedia = $state(false);
  let syncBanner = $state<"resync" | "sync" | null>(null);

  const mainServer = $derived(servers.find((entry) => entry.config.isMain));
  const busy = $derived(deletingServer || promoting);

  const displayName = (server: MediaServerState): string =>
    `${SERVER_LABELS[server.serverKey]} - ${server.config.name}`;

  // main first, then grouped by type in the order they are offered, then by
  // name - so a card never jumps around after an unrelated save
  const sortServers = (entries: MediaServerState[]): MediaServerState[] =>
    [...entries].sort((a, b) => {
      if (a.config.isMain !== b.config.isMain) return a.config.isMain ? -1 : 1;
      if (a.serverKey !== b.serverKey)
        return SERVERS.indexOf(a.serverKey) - SERVERS.indexOf(b.serverKey);
      return a.config.name.localeCompare(b.config.name);
    });

  // names in use by other servers of the same type. The backend keys a config
  // by (type, name), so reusing one would overwrite that server instead of
  // adding a new one.
  const takenNamesFor = (
    serverKey: ServerKey,
    excludeId: number | null,
  ): string[] =>
    servers
      .filter(
        (entry) =>
          entry.serverKey === serverKey &&
          (excludeId === null || entry.config.id !== excludeId),
      )
      .map((entry) => entry.config.name);

  const suggestedNameFor = (serverKey: ServerKey): string => {
    const taken = new Set(
      takenNamesFor(serverKey, null).map((name) => name.toLowerCase()),
    );
    const base = SERVER_LABELS[serverKey];
    if (!taken.has(base.toLowerCase())) return base;
    for (let suffix = 2; ; suffix += 1) {
      const candidate = `${base} ${suffix}`;
      if (!taken.has(candidate.toLowerCase())) return candidate;
    }
  };

  // load media server settings on mount
  const loadSettings = async () => {
    try {
      loading = true;
      const rawServices = await get_api<
        Record<
          string,
          {
            instances?: Array<{
              id?: number;
              name?: string;
              enabled: boolean;
              is_main: boolean | null;
              base_url: string;
              api_key: string;
              last_synced_at?: string | null;
            }>;
          }
        >
      >("/api/settings/services");

      const next: MediaServerState[] = [];
      for (const serverKey of SERVERS) {
        for (const raw of rawServices[serverKey]?.instances ?? []) {
          if (raw.id === undefined || raw.id === null) continue;
          const config: MediaServerConfig = {
            id: raw.id,
            name: raw.name || SERVER_LABELS[serverKey],
            enabled: raw.enabled,
            baseUrl: raw.base_url,
            isMain: raw.is_main ?? false,
          };
          next.push({
            serverKey,
            config,
            apiKeyIsSet: !!raw.api_key,
            syncing: false,
            lastSyncedAt: raw.last_synced_at ?? null,
          });
        }
      }
      servers = sortServers(next);
    } catch (err: any) {
      toast.warning(`Error loading media server settings: ${err.message}`);
    } finally {
      loading = false;
    }
  };

  const openAddDialog = () => {
    dialogTarget = { serverKey: addServerKey, existing: null };
    dialogOpen = true;
  };

  const openEditDialog = (server: MediaServerState) => {
    dialogTarget = { serverKey: server.serverKey, existing: server };
    dialogOpen = true;
  };

  // a save can flip which server is main and clear the flag on the old one, so
  // reload rather than patching the row we happen to know about
  const handleSaved = async (response: SaveServiceResponse) => {
    syncBanner = response.sync_action === "resync" ? "resync" : "sync";
    await loadSettings();
  };

  const promoteToMain = async () => {
    const target = promoteTarget;
    if (!target || target.config.id === null) return;
    promoting = true;
    try {
      // the api key is never held client side; omitting it tells the backend to
      // reuse the stored one for this id
      const response = await post_api<SaveServiceResponse>(
        "/api/settings/save/service",
        {
          id: target.config.id,
          name: target.config.name,
          service_type: target.serverKey,
          enabled: true,
          base_url: target.config.baseUrl,
          is_main: true,
        },
      );
      promoteTarget = null;
      toast.success(`${displayName(target)} is now the main server`);
      await handleSaved(response);
    } catch (err: any) {
      toast.error(`Could not switch the main server: ${err.message}`);
    } finally {
      promoting = false;
    }
  };

  const deleteServer = async () => {
    const target = deleteTarget;
    if (!target || target.config.id === null) return;
    deletingServer = true;
    try {
      const response: {
        message: string;
        data: { removed_path_mappings?: number };
      } = await delete_api(`/api/settings/service/${target.config.id}`);
      servers = servers.filter((entry) => entry.config.id !== target.config.id);
      deleteTarget = null;
      toast.success(response.message);
      const removedMappings = response.data.removed_path_mappings ?? 0;
      if (removedMappings) {
        toast.warning(
          `${removedMappings} scoped path mapping(s) were removed.`,
          { duration: 8000 },
        );
      }
    } catch (err: any) {
      toast.error(`Error deleting ${displayName(target)}: ${err.message}`);
    } finally {
      deletingServer = false;
    }
  };

  const sleep = (ms: number) =>
    new Promise((resolve) => window.setTimeout(resolve, ms));

  const watchSyncJob = async (jobId: number) => {
    for (let attempt = 0; attempt < 80; attempt += 1) {
      const job = await get_api<BackgroundJobPollResponse>(
        `/api/tasks/background-jobs/${jobId}`,
      );

      if (job.status === "completed") {
        const affectedRules =
          job.payload?.result?.library_sync?.affected_rules ?? [];
        if (affectedRules.length > 0) {
          toast.warning(
            `Media sync updated ${affectedRules.length} rule(s) because some libraries no longer exist.`,
            { duration: 8000 },
          );
        }
        return;
      }

      if (job.status === "failed" || job.status === "canceled") {
        if (job.error_message) {
          toast.error(`Media sync failed: ${job.error_message}`);
        }
        return;
      }

      await sleep(1500);
    }
  };

  // trigger a full media sync via the tasks API
  const syncMedia = async () => {
    syncingMedia = true;
    try {
      const response = await post_api<SyncMediaRunResponse>(
        "/api/tasks/tasks/sync_media/run",
      );

      if (response.job_id !== null) {
        void watchSyncJob(response.job_id);
      }

      if (response.already_active) {
        toast.info(response.message);
      } else {
        toast.success(
          "Media sync started! Check the Tasks page to monitor progress.",
        );
      }
      syncBanner = null;
    } catch (err: any) {
      toast.error(`Failed to start sync: ${err.message}`);
    } finally {
      syncingMedia = false;
    }
  };

  // sync one media server. Main runs the full media sync (it owns library and
  // version rows); a linked server only refreshes its own watch + supplemental
  // data, so it never waits on, or blocks, the others.
  const syncServer = async (server: MediaServerState) => {
    const configId = server.config.id;
    if (configId === null) return;

    server.syncing = true;
    try {
      const response = await post_api<MediaServerSyncResponse>(
        `/api/settings/media-servers/${configId}/sync`,
      );

      if (response.scope === "main" && response.job_id !== null) {
        void watchSyncJob(response.job_id);
      }

      if (response.already_active) {
        toast.info(response.message);
      } else {
        toast.success(
          `${response.message}. Check the Tasks page to monitor progress.`,
        );
      }
    } catch (err: any) {
      toast.error(`Failed to start sync: ${err.message}`);
    } finally {
      server.syncing = false;
    }
  };

  onMount(async () => {
    await loadSettings();
  });
</script>

{#if loading}
  <div class="flex items-center justify-center gap-3 text-muted-foreground p-8">
    <Spinner class="size-5 text-primary" />
    Loading...
  </div>
{:else}
  <div class="space-y-6">
    <div>
      <h2 class="text-lg flex items-center font-semibold text-foreground">
        <Server class="size-4 mr-2" />
        Media Servers
      </h2>
      <p class="text-sm text-muted-foreground mt-0.5">
        Add one or more media servers. Exactly one is the
        <strong>main server</strong>, the source of every library and media
        record. Every other server - including another instance of the same type
        - is <strong>linked</strong> and contributes watch history and user data only.
      </p>
    </div>

    <Notice title={"Tip"}>
      <p>
        For the best experience, configure your <strong>*Arr</strong>
        applications,
        <strong>Seerr</strong>, and <strong>Tautulli</strong>
        <i>(if desired)</i> before running your first sync. This helps
        Reclaimerr build a complete view of your media, requests, and playback
        history.
        <br />
        <br />
        <i
          >Note: If you configure these later you will simply just have to wait
          for or run the
          <strong>Sync Media</strong> task after
        </i>
      </p>
    </Notice>

    <!-- add a server -->
    <div
      class="flex flex-wrap items-end justify-between gap-3 rounded-md border
        border-border bg-background/50 p-3"
    >
      <div class="flex flex-col gap-2">
        <Label
          for="add-server-type"
          class="text-sm font-medium text-foreground"
        >
          Server type
        </Label>
        <Select.Root
          name="add-server-type"
          type="single"
          value={addServerKey}
          onValueChange={(value) => (addServerKey = value as ServerKey)}
        >
          <Select.Trigger size="sm" class="w-52 cursor-pointer text-foreground">
            {@const Icon = SERVER_ICONS[addServerKey]}
            <span class="flex items-center gap-2">
              <Icon class="size-4 shrink-0" />
              {SERVER_LABELS[addServerKey]}
            </span>
          </Select.Trigger>
          <Select.Content>
            {#each SERVERS as serverKey (serverKey)}
              {@const Icon = SERVER_ICONS[serverKey]}
              <Select.Item
                value={serverKey}
                label={SERVER_LABELS[serverKey]}
                class="cursor-pointer"
              >
                <span class="flex items-center gap-2">
                  <Icon class="size-4 shrink-0" />
                  {SERVER_LABELS[serverKey]}
                </span>
              </Select.Item>
            {/each}
          </Select.Content>
        </Select.Root>
      </div>
      <Button
        id="add-media-server-button"
        size="sm"
        class="cursor-pointer gap-1.5"
        disabled={busy}
        onclick={openAddDialog}
      >
        <Plus class="size-3.5" />
        Add server
      </Button>
    </div>

    <!-- configured servers -->
    <section class="space-y-3">
      <div class="flex items-baseline justify-between gap-3">
        <h3 class="font-semibold text-foreground">
          Configured servers
          {#if servers.length}
            <span class="text-sm font-normal text-muted-foreground"
              >({servers.length})</span
            >
          {/if}
        </h3>
        {#if servers.length > 1}
          <p class="text-xs text-muted-foreground">
            Only the main server provides libraries.
          </p>
        {/if}
      </div>

      {#if servers.length === 0}
        <div
          class="rounded-lg border border-dashed border-border p-8 text-center"
        >
          <Server class="mx-auto size-6 text-muted-foreground" />
          <p class="mt-2 text-sm text-muted-foreground">
            No media servers yet. Pick a type above and add your first one - it
            becomes the main server.
          </p>
        </div>
      {:else}
        {#if !mainServer}
          <div
            class="flex items-start gap-2 p-3 rounded-md bg-warning/50 border
              border-warning-secondary text-warning-foreground"
          >
            <AlertTriangle class="size-4 mt-0.5 shrink-0" />
            <p class="text-sm">
              No main server is set, so no libraries or media can be synced.
              Choose <strong>Set as main</strong> on one of the servers below.
            </p>
          </div>
        {/if}
        <div class="space-y-3">
          {#each servers as server (server.config.id)}
            <MediaServerCard
              {server}
              label={SERVER_LABELS[server.serverKey]}
              icon={SERVER_ICONS[server.serverKey]}
              {busy}
              onEdit={() => openEditDialog(server)}
              onSetMain={() => (promoteTarget = server)}
              onSync={() => syncServer(server)}
              onDelete={() => (deleteTarget = server)}
            />
          {/each}
        </div>
      {/if}
    </section>

    <!-- post save sync banner -->
    {#if syncBanner}
      <div
        class="flex relative items-start justify-between gap-3 rounded-md border p-4
          {syncBanner === 'resync'
          ? 'bg-warning/20 border-warning-secondary text-warning-foreground'
          : 'bg-primary/10 border-primary/30 text-foreground'}"
      >
        <div class="flex-1 space-y-2">
          {#if syncBanner === "resync"}
            <p class="text-sm font-medium">
              Full resync triggered for the new main server.
            </p>
            <p class="text-xs text-muted-foreground">
              Old media data is being replaced. Check the <strong>Tasks</strong>
              page to monitor progress.
            </p>
          {:else}
            <p class="text-sm font-medium">
              Settings saved - you can sync now to get the latest media data or
              wait for the next automatic sync.
            </p>
            {#if servers.length === 1}
              <p class="text-xs text-muted-foreground">
                Tip: you can also add a linked server to bring in watch history
                and user data before syncing.
              </p>
            {/if}
            <Button
              size="sm"
              class="cursor-pointer gap-2 mt-1"
              disabled={syncingMedia}
              onclick={syncMedia}
            >
              {#if syncingMedia}
                <Spinner class="size-4" />
                Starting...
              {:else}
                <RefreshCw class="size-4" />
                Sync Media Now
              {/if}
            </Button>
          {/if}
        </div>
        <Button
          variant="ghost"
          class="absolute top-1 right-1 cursor-pointer text-muted-foreground hover:text-foreground"
          onclick={() => (syncBanner = null)}
          aria-label="Dismiss"
        >
          <X class="size-4" />
        </Button>
      </div>
    {/if}
  </div>
{/if}

{#if dialogTarget}
  <MediaServerDialog
    bind:open={dialogOpen}
    serverKey={dialogTarget.serverKey}
    label={SERVER_LABELS[dialogTarget.serverKey]}
    icon={SERVER_ICONS[dialogTarget.serverKey]}
    urlPlaceholder={SERVER_URL_PLACEHOLDERS[dialogTarget.serverKey]}
    apiKeyLabel={apiKeyLabelFor(dialogTarget.serverKey)}
    existing={dialogTarget.existing}
    willBeMain={dialogTarget.existing
      ? dialogTarget.existing.config.isMain
      : !mainServer}
    takenNames={takenNamesFor(
      dialogTarget.serverKey,
      dialogTarget.existing?.config.id ?? null,
    )}
    suggestedName={suggestedNameFor(dialogTarget.serverKey)}
    onSaved={handleSaved}
  />
{/if}

<!-- switch main server -->
<AlertDialog.Root
  open={promoteTarget !== null}
  onOpenChange={(open) => {
    if (!open && !promoting) promoteTarget = null;
  }}
>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>
        {mainServer ? "Switch the main server?" : "Set the main server?"}
      </AlertDialog.Title>
      <AlertDialog.Description>
        {#if promoteTarget}
          {#if mainServer}
            <strong>{displayName(promoteTarget)}</strong> replaces
            <strong>{displayName(mainServer)}</strong>
            as the source of library and media data, which triggers a full resync
            and may take a while. Rules scoped to a library the new server does not
            have are reported as stale rather than silently retargeted.
          {:else}
            <strong>{displayName(promoteTarget)}</strong> becomes the source of library
            and media data, and a full media sync is started.
          {/if}
          {#if !promoteTarget.config.enabled}
            <br /><br />
            This server is currently disabled and will be enabled, so it must be reachable
            right now.
          {/if}
        {/if}
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel disabled={promoting}>Cancel</AlertDialog.Cancel>
      <AlertDialog.Action disabled={promoting} onclick={promoteToMain}>
        {promoting ? "Switching..." : "Set as main"}
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<!-- delete a linked server -->
<AlertDialog.Root
  open={deleteTarget !== null}
  onOpenChange={(open) => {
    if (!open && !deletingServer) deleteTarget = null;
  }}
>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Delete linked media server?</AlertDialog.Title>
      <AlertDialog.Description>
        {#if deleteTarget}
          Permanently delete <strong>{displayName(deleteTarget)}</strong>? This
          cannot be undone.
        {/if}
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel disabled={deletingServer}>Cancel</AlertDialog.Cancel>
      <AlertDialog.Action
        disabled={deletingServer}
        onclick={deleteServer}
        class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
      >
        {deletingServer ? "Deleting..." : "Delete"}
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
