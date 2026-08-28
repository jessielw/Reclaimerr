<script lang="ts">
  import { onMount } from "svelte";
  import { delete_api, get_api, post_api } from "$lib/api";
  import ServiceConfigForm from "$lib/components/settings/service-config-form.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import JellyfinSVG from "$lib/components/svgs/jellyfin-svg.svelte";
  import EmbySVG from "$lib/components/svgs/emby-svg.svelte";
  import PlexSVG from "$lib/components/svgs/plex-svg.svelte";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import TestButton from "$lib/components/test-button.svelte";
  import Save from "@lucide/svelte/icons/save";
  import X from "@lucide/svelte/icons/x";
  import AlertTriangle from "@lucide/svelte/icons/triangle-alert";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import Plus from "@lucide/svelte/icons/plus";
  import Server from "@lucide/svelte/icons/server";
  import BadgeCheck from "@lucide/svelte/icons/badge-check";
  import { toast } from "svelte-sonner";
  import { SettingsTab } from "$lib/types/shared";
  import { formatDistanceToNow } from "$lib/utils/date";
  import * as Select from "$lib/components/ui/select/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import Checkbox from "$lib/components/ui/checkbox/checkbox.svelte";
  import Notice from "$lib/components/notice.svelte";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import { MEDIA_SERVERS as SERVERS } from "$lib/types/shared";

  type ServerKey = (typeof SERVERS)[number];

  type MediaServerConfig = {
    id: number | null;
    name: string;
    enabled: boolean;
    baseUrl: string;
    apiKey: string;
    isMain: boolean;
  };

  type MediaServerSyncResponse = SyncMediaRunResponse & {
    task: string;
    scope: "main" | "linked";
  };

  // localId is a stable, client-side-only identity for one instance (a draft
  // or a loaded row) - independent of the server-assigned `id`, which is
  // null until the first successful save. Every selection/diffing concern
  // (which instance is pending main, has it been edited, etc.) keys off
  // localId rather than server id, since a brand new draft has no id yet
  // but still needs to be individually addressable.
  type MediaServerState = {
    localId: string;
    config: MediaServerConfig;
    original: MediaServerConfig;
    apiKeyIsSet: boolean;
    testing: boolean;
    saving: boolean;
    syncing: boolean;
    lastSyncedAt: string | null;
    testStatus: "idle" | "loading" | "success" | "error";
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

  const newLocalId = (): string =>
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `local-${Date.now()}-${Math.random().toString(36).slice(2)}`;

  const emptyConfig = (serverKey: ServerKey): MediaServerConfig => ({
    id: null,
    name: SERVER_LABELS[serverKey],
    enabled: false,
    baseUrl: "",
    apiKey: "",
    isMain: false,
  });

  const emptyState = (serverKey: ServerKey): MediaServerState => {
    const config = emptyConfig(serverKey);
    return {
      localId: newLocalId(),
      config,
      original: { ...config },
      apiKeyIsSet: false,
      testing: false,
      saving: false,
      syncing: false,
      lastSyncedAt: null,
      testStatus: "idle",
    };
  };

  let servers = $state<Record<ServerKey, MediaServerState[]>>({
    jellyfin: [],
    emby: [],
    plex: [],
  });

  let loading = $state(false);
  let globalSaving = $state(false);
  let syncingMedia = $state(false);
  let confirmServerChange = $state(false);
  let syncBanner = $state<"resync" | "sync" | null>(null);
  let deleteTarget = $state<{ serverKey: ServerKey; localId: string } | null>(
    null,
  );
  let deletingServer = $state(false);

  // stores each instance's enabled state from just before it was promoted to
  // main, so we can restore it if it gets demoted back to linked
  let enabledBeforePromotion = $state<Record<string, boolean>>({});

  // the saved main instance's localId, so we can warn when the user changes it
  let savedMainLocalId = $state<string | null>(null);

  // the pending dropdown selection (may differ from saved)
  let pendingMainLocalId = $state<string | null>(null);

  // flatten every instance across every type, in a stable (type, insertion) order
  const allInstances = $derived<
    { serverKey: ServerKey; state: MediaServerState }[]
  >(
    SERVERS.flatMap((serverKey) =>
      servers[serverKey].map((state) => ({ serverKey, state })),
    ),
  );

  const findInstance = (
    localId: string | null,
  ): { serverKey: ServerKey; state: MediaServerState } | null => {
    if (localId === null) return null;
    return (
      allInstances.find((entry) => entry.state.localId === localId) ?? null
    );
  };

  const pendingMainEntry = $derived(findInstance(pendingMainLocalId));

  const mainServerChanged = $derived(
    savedMainLocalId !== null &&
      pendingMainLocalId !== null &&
      pendingMainLocalId !== savedMainLocalId,
  );

  const anyInstanceConfigured = $derived(allInstances.length > 0);

  // handle changes from the config forms
  const handleConfigChange = (
    serverKey: ServerKey,
    localId: string,
    event: CustomEvent,
  ) => {
    const { field, value } = event.detail;
    const list = servers[serverKey];
    const idx = list.findIndex((s) => s.localId === localId);
    if (idx === -1) return;
    list[idx].testStatus = "idle";
    if (field === "enabled") list[idx].config.enabled = value;
    else if (field === "name") list[idx].config.name = value;
    else if (field === "baseUrl") list[idx].config.baseUrl = value;
    else if (field === "apiKey") list[idx].config.apiKey = value;
  };

  // test connection to a media server instance
  const testServer = async (serverKey: ServerKey, localId: string) => {
    const list = servers[serverKey];
    const idx = list.findIndex((s) => s.localId === localId);
    if (idx === -1) return;
    list[idx].testing = true;
    list[idx].testStatus = "loading";
    const config = list[idx].config;
    try {
      const payload: Record<string, unknown> = {
        service_type: serverKey,
        enabled: config.enabled,
        base_url: config.baseUrl,
      };
      if (config.apiKey) payload.api_key = config.apiKey;
      const response: boolean = await post_api(
        "/api/settings/test/service",
        payload,
      );
      if (!response) throw new Error("Connection test failed");
      list[idx].testStatus = "success";
    } catch (err: any) {
      list[idx].testStatus = "error";
      toast.error(
        `Connection test for ${SERVER_LABELS[serverKey]} (${config.name}) failed: ${err.message}`,
      );
    } finally {
      list[idx].testing = false;
    }
  };

  // save a single instance's settings (returns the sync_action from the API response)
  const saveServer = async (
    serverKey: ServerKey,
    localId: string,
  ): Promise<"resync" | "sync" | null> => {
    const list = servers[serverKey];
    const idx = list.findIndex((s) => s.localId === localId);
    if (idx === -1) return null;
    list[idx].saving = true;
    const config = { ...list[idx].config };
    try {
      const response: {
        message: string;
        sync_action: "resync" | "sync" | null;
        data: {
          id: number;
          name: string;
          service_type: string;
          enabled: boolean;
          base_url: string;
          is_main: boolean;
        };
      } = await post_api("/api/settings/save/service", {
        id: config.id,
        name: config.name,
        service_type: serverKey,
        enabled: config.enabled,
        base_url: config.baseUrl,
        is_main: config.isMain,
        ...(config.apiKey ? { api_key: config.apiKey } : {}),
      });
      const saved: MediaServerConfig = {
        id: response.data.id,
        name: response.data.name,
        enabled: response.data.enabled,
        baseUrl: response.data.base_url,
        apiKey: "",
        isMain: response.data.is_main,
      };
      list[idx].config = saved;
      list[idx].original = { ...saved };
      list[idx].apiKeyIsSet = true;
      if (response.data.is_main) {
        savedMainLocalId = localId;
        pendingMainLocalId = localId;
        for (const entry of allInstances) {
          if (entry.state.localId !== localId) {
            entry.state.config.isMain = false;
          }
        }
      }
      toast.success(response.message);
      return response.sync_action ?? null;
    } catch (err: any) {
      toast.error(
        `Error saving ${SERVER_LABELS[serverKey]} (${config.name}) settings: ${err.message}`,
      );
      return null;
    } finally {
      list[idx].saving = false;
    }
  };

  // remove an unsaved draft locally (no server-side delete needed)
  const removeDraft = (serverKey: ServerKey, localId: string) => {
    servers[serverKey] = servers[serverKey].filter(
      (s) => s.localId !== localId,
    );
    if (pendingMainLocalId === localId) pendingMainLocalId = savedMainLocalId;
  };

  const deleteServerInstance = async () => {
    const target = deleteTarget;
    if (!target) return;
    const list = servers[target.serverKey];
    const idx = list.findIndex((s) => s.localId === target.localId);
    if (idx === -1) {
      deleteTarget = null;
      return;
    }
    const configId = list[idx].config.id;
    if (!configId) {
      removeDraft(target.serverKey, target.localId);
      deleteTarget = null;
      return;
    }

    deletingServer = true;
    try {
      const response: {
        message: string;
        data: { removed_path_mappings?: number };
      } = await delete_api(`/api/settings/service/${configId}`);
      servers[target.serverKey] = list.filter(
        (s) => s.localId !== target.localId,
      );
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
      toast.error(
        `Error deleting ${SERVER_LABELS[target.serverKey]}: ${err.message}`,
      );
    } finally {
      deletingServer = false;
    }
  };

  const instanceIsDirty = (state: MediaServerState): boolean => {
    const { config, original } = state;
    if (!config.id) {
      return !!(
        config.baseUrl.trim() ||
        config.apiKey.trim() ||
        config.name.trim() !== original.name.trim() ||
        config.enabled
      );
    }
    return (
      !!config.apiKey ||
      config.name !== original.name ||
      config.enabled !== original.enabled ||
      config.baseUrl !== original.baseUrl ||
      config.isMain !== original.isMain
    );
  };

  // save all instances, prioritizing the pending main instance first
  const saveAll = async () => {
    globalSaving = true;
    syncBanner = null;
    const mainLocalId = pendingMainLocalId; // capture before async ops
    try {
      const ordered = [...allInstances].sort((a, b) => {
        if (a.state.localId === mainLocalId) return -1;
        if (b.state.localId === mainLocalId) return 1;
        return 0;
      });
      let anySaved = false;
      for (const { serverKey, state } of ordered) {
        const { config } = state;
        if (config.baseUrl && instanceIsDirty(state)) {
          const action = await saveServer(serverKey, state.localId);
          anySaved = true;
          // resync takes priority (main server was swapped, background task running)
          if (state.localId === mainLocalId && action === "resync") {
            syncBanner = "resync";
          }
        }
      }
      // show the sync banner after any successful save (unless resync already set)
      if (anySaved && syncBanner === null) {
        syncBanner = "sync";
      } else if (!anySaved) {
        toast.info("No changes to save.");
      }
    } finally {
      globalSaving = false;
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

  // trigger a media sync via the tasks API
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
  const syncServer = async (localId: string) => {
    const entry = findInstance(localId);
    if (!entry) return;
    const { state } = entry;
    const configId = state.config.id;
    if (configId === null) {
      toast.info("Save this server before syncing it.");
      return;
    }

    state.syncing = true;
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
      state.syncing = false;
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

      const next: Record<ServerKey, MediaServerState[]> = {
        jellyfin: [],
        emby: [],
        plex: [],
      };
      savedMainLocalId = null;
      pendingMainLocalId = null;
      enabledBeforePromotion = {};

      for (const serverKey of SERVERS) {
        const instances = rawServices[serverKey]?.instances ?? [];
        for (const raw of instances) {
          const config: MediaServerConfig = {
            id: raw.id ?? null,
            name: raw.name || SERVER_LABELS[serverKey],
            enabled: raw.enabled,
            baseUrl: raw.base_url,
            apiKey: "",
            isMain: raw.is_main ?? false,
          };
          const localId = newLocalId();
          next[serverKey].push({
            localId,
            config,
            original: { ...config },
            apiKeyIsSet: !!raw.api_key,
            testing: false,
            saving: false,
            syncing: false,
            lastSyncedAt: raw.last_synced_at ?? null,
            testStatus: "idle",
          });
          enabledBeforePromotion[localId] = raw.enabled;
          if (raw.is_main) {
            savedMainLocalId = localId;
            pendingMainLocalId = localId;
          }
        }
      }
      servers = next;
    } catch (err: any) {
      toast.warning(`Error loading media server settings: ${err.message}`);
    } finally {
      loading = false;
    }
  };

  const addInstance = (serverKey: ServerKey) => {
    servers[serverKey] = [...servers[serverKey], emptyState(serverKey)];
  };

  const selectMainServer = (localId: string) => {
    const oldLocalId = pendingMainLocalId;
    if (oldLocalId && oldLocalId !== localId) {
      const oldEntry = findInstance(oldLocalId);
      if (oldEntry) {
        // restore the demoted instance's enabled state from before it was promoted
        oldEntry.state.config.enabled =
          enabledBeforePromotion[oldLocalId] ?? false;
      }
    }

    const newEntry = findInstance(localId);
    if (!newEntry) return;

    // save the incoming instance's enabled state before forcing it on
    enabledBeforePromotion[localId] = newEntry.state.config.enabled;

    pendingMainLocalId = localId;
    for (const entry of allInstances) {
      entry.state.config.isMain = entry.state.localId === localId;
    }
    // main server must always be enabled
    newEntry.state.config.enabled = true;
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
  <div class="space-y-8">
    <section class="space-y-4">
      <div>
        <h2 class="text-lg flex items-center font-semibold text-foreground">
          <Server class="size-4 mr-2" />
          Media Servers
        </h2>
        <p class="text-sm text-muted-foreground mt-0.5">
          Configure one or more media servers. One instance must be selected as
          the <strong>main server</strong>, which is the primary source for
          library and media data. Every other instance - including additional
          instances of the same type - is <strong>linked</strong>
          for watch history and user data only.
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
            >Note: If you configure these later you will simply just have to
            wait for or run the
            <strong>Sync Media</strong> task after
          </i>
        </p>
      </Notice>

      <hr />

      <!-- main Server -->
      <div>
        <h3 class="font-semibold text-foreground">Main Server</h3>
        <p class="text-sm text-muted-foreground mt-0.5">
          The primary source for library and media sync. Pick a specific
          instance, even if it's not the only one of its type. Every library a
          rule can be scoped to comes from this server and is labelled with its
          name; linked servers contribute watch history, not library contents.
        </p>
        <p class="text-sm text-muted-foreground mt-1.5">
          Changing the main server replaces those libraries with the new
          server's. Rules scoped to a library the new server does not have are
          reported as stale rather than silently retargeted.
        </p>
      </div>

      <!-- dropdown to select main server, across every instance of every type -->
      <div class="flex flex-col gap-2 max-w-xs">
        <Label
          for="main-server-select"
          class="text-sm font-medium text-foreground"
        >
          Select Main Server
        </Label>
        <div class="w-1/2">
          <Select.Root
            name="main-server-select"
            type="single"
            value={pendingMainLocalId ?? undefined}
            onValueChange={(value) => selectMainServer(value)}
          >
            <Select.Trigger class="w-full cursor-pointer text-foreground">
              {#if pendingMainEntry}
                {@const Icon = SERVER_ICONS[pendingMainEntry.serverKey]}
                <span class="flex items-center gap-2">
                  <Icon class="size-4 shrink-0" />
                  {SERVER_LABELS[pendingMainEntry.serverKey]} - {pendingMainEntry
                    .state.config.name}
                </span>
              {:else}
                <span class="text-muted-foreground">Select a server...</span>
              {/if}
            </Select.Trigger>
            <Select.Content>
              {#each allInstances as { serverKey, state } (state.localId)}
                {@const Icon = SERVER_ICONS[serverKey]}
                <Select.Item value={state.localId} class="cursor-pointer">
                  <span class="flex items-center gap-2">
                    <Icon class="size-4 shrink-0" />
                    {SERVER_LABELS[serverKey]} - {state.config.name}
                    {#if state.localId === savedMainLocalId}
                      <span class="text-xs text-muted-foreground"
                        >(current)</span
                      >
                    {/if}
                  </span>
                </Select.Item>
              {/each}
            </Select.Content>
          </Select.Root>
        </div>
      </div>

      <!-- warning banner when changing main server -->
      {#if mainServerChanged}
        {@const savedEntry = findInstance(savedMainLocalId)}
        <div
          class="flex flex-col items-start gap-3 p-3 rounded-md bg-warning/50 border border-warning-secondary
            text-warning-foreground"
        >
          <div class="flex items-start gap-2">
            <AlertTriangle class="size-4 mt-0.5 shrink-0" />
            <p class="text-sm">
              Changing the main server from <strong
                >{savedEntry
                  ? `${SERVER_LABELS[savedEntry.serverKey]} - ${savedEntry.state.config.name}`
                  : "none"}</strong
              >
              to
              <strong
                >{pendingMainEntry
                  ? `${SERVER_LABELS[pendingMainEntry.serverKey]} - ${pendingMainEntry.state.config.name}`
                  : ""}</strong
              > will trigger a full media resync. This may take a while. Make sure
              the new server is fully configured before saving.
            </p>
          </div>
          <div class="flex items-center gap-2">
            <Checkbox
              id="confirm-main-server-change"
              class="bg-white! data-[state=checked]:bg-primary!
              data-[state=checked]:border-primary! border cursor-pointer"
              bind:checked={confirmServerChange}
            />
            <Label
              for="confirm-main-server-change"
              class="text-sm cursor-pointer font-bold"
            >
              I understand and want to proceed
            </Label>
          </div>
        </div>
      {/if}
    </section>

    <!-- one section per media-server type, each listing every instance of that type -->
    {#each SERVERS as serverKey (serverKey)}
      {@const Icon = SERVER_ICONS[serverKey]}
      <hr />
      <section class="space-y-4">
        <div class="flex items-center justify-between">
          <h2 class="font-semibold text-foreground flex items-center gap-2">
            <Icon class="size-4 shrink-0" />
            {SERVER_LABELS[serverKey]}
          </h2>
          <Button
            size="sm"
            variant="outline"
            class="cursor-pointer gap-1.5"
            onclick={() => addInstance(serverKey)}
          >
            <Plus class="size-3.5" />
            Add another
          </Button>
        </div>

        {#if servers[serverKey].length === 0}
          <p class="text-sm text-muted-foreground">Not configured.</p>
        {:else}
          <div class="space-y-4">
            {#each servers[serverKey] as state (state.localId)}
              {@const apiKeyLabel =
                serverKey === SettingsTab.Plex ? "Token" : "API Key"}
              {@const isPendingMain = state.localId === pendingMainLocalId}
              <div class="rounded-lg border border-border p-5 space-y-4">
                <div class="flex items-center justify-between gap-2">
                  {#if isPendingMain}
                    <span
                      class="flex items-center gap-1.5 text-xs font-medium text-primary"
                    >
                      <BadgeCheck class="size-3.5" />
                      Main server
                    </span>
                  {:else}
                    <span class="text-xs text-muted-foreground">Linked</span>
                  {/if}
                  {#if state.config.id !== null}
                    <span class="text-xs text-muted-foreground truncate">
                      Last sync: {state.lastSyncedAt
                        ? formatDistanceToNow(state.lastSyncedAt)
                        : "never"}
                    </span>
                  {/if}
                </div>
                <ServiceConfigForm
                  tabLabel={SERVER_LABELS[serverKey]}
                  tabIcon={SERVER_ICONS[serverKey]}
                  enabled={state.config.enabled}
                  name={state.config.name}
                  baseUrl={state.config.baseUrl}
                  apiKey={state.config.apiKey}
                  apiKeyIsSet={state.apiKeyIsSet}
                  {apiKeyLabel}
                  baseUrlPlaceholder={SERVER_URL_PLACEHOLDERS[serverKey]}
                  disableToggle={isPendingMain}
                  onchange={(e) =>
                    handleConfigChange(serverKey, state.localId, e)}
                />
                <div class="flex gap-2 justify-end">
                  {#if !isPendingMain}
                    <Button
                      size="sm"
                      class="cursor-pointer gap-2 bg-destructive/80 hover:bg-destructive text-destructive-foreground"
                      disabled={state.saving || globalSaving}
                      onclick={() =>
                        (deleteTarget = { serverKey, localId: state.localId })}
                    >
                      <Trash2 class="size-4" />
                      {state.config.id ? "Delete" : "Remove"}
                    </Button>
                  {/if}
                  {#if state.config.id !== null}
                    <Button
                      size="sm"
                      variant="outline"
                      class="cursor-pointer gap-2"
                      disabled={state.syncing ||
                        state.saving ||
                        globalSaving ||
                        mainServerChanged ||
                        !state.original.enabled}
                      title={!state.original.enabled
                        ? "Enable and save this server to sync it"
                        : mainServerChanged
                          ? "Save the main server change first"
                          : state.original.isMain
                            ? "Run a full media sync from this server"
                            : "Refresh watch data from this server"}
                      onclick={() => syncServer(state.localId)}
                    >
                      {#if state.syncing}
                        <Spinner class="size-4" />
                      {:else}
                        <RefreshCw class="size-4" />
                      {/if}
                      Sync
                    </Button>
                  {/if}
                  <TestButton
                    onclick={() => testServer(serverKey, state.localId)}
                    disabled={state.testing || state.saving || globalSaving}
                    status={state.testStatus}
                    class="cursor-pointer gap-2"
                    size="sm">Test</TestButton
                  >
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </section>
    {/each}

    <!-- save + sync banner -->
    {#if anyInstanceConfigured}
      <hr />
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
                Old media data is being replaced. Check the <strong
                  >Tasks</strong
                > page to monitor progress.
              </p>
            {:else}
              <p class="text-sm font-medium">
                Settings saved - you can sync now to get the latest media data
                or wait for the next automatic sync.
              </p>
              {#if allInstances.every((entry) => entry.state.localId === pendingMainLocalId || !entry.state.apiKeyIsSet)}
                <p class="text-xs text-muted-foreground">
                  Tip: you can also configure a linked server to bring in watch
                  history and user data before syncing.
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

      <!-- save all button -->
      <div class="flex justify-end pt-4">
        <Button
          id="save-all-button"
          onclick={saveAll}
          disabled={globalSaving || (mainServerChanged && !confirmServerChange)}
          class="cursor-pointer gap-2"
        >
          {#if globalSaving}
            <Spinner class="size-4" />
            Saving...
          {:else}
            <Save class="size-4" /> Save
          {/if}
        </Button>
      </div>
    {/if}
  </div>
{/if}

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
          Permanently delete the {SERVER_LABELS[deleteTarget.serverKey]} configuration?
          This cannot be undone.
        {/if}
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel disabled={deletingServer}>Cancel</AlertDialog.Cancel>
      <AlertDialog.Action
        disabled={deletingServer}
        onclick={deleteServerInstance}
        class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
      >
        {deletingServer ? "Deleting..." : "Delete"}
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
