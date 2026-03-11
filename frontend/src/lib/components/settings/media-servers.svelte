<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import ServiceConfigForm from "$lib/components/settings/service-config-form.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import JellyfinSVG from "$lib/components/svgs/JellyfinSVG.svelte";
  import PlexSVG from "$lib/components/svgs/PlexSVG.svelte";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import TestButton from "$lib/components/test-button.svelte";
  import Save from "@lucide/svelte/icons/save";
  import X from "@lucide/svelte/icons/x";
  import AlertTriangle from "@lucide/svelte/icons/triangle-alert";
  import Server from "@lucide/svelte/icons/server";
  import { toast } from "svelte-sonner";
  import { SettingsTab } from "$lib/types/shared";
  import * as Select from "$lib/components/ui/select/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import Checkbox from "$lib/components/ui/checkbox/checkbox.svelte";
  import { MEDIA_SERVERS as SERVERS } from "$lib/types/shared";

  type ServerKey = (typeof SERVERS)[number];

  type MediaServerConfig = {
    enabled: boolean;
    baseUrl: string;
    apiKey: string;
    isMain: boolean;
  };

  type MediaServerState = {
    config: MediaServerConfig;
    apiKeyIsSet: boolean;
    testing: boolean;
    saving: boolean;
  };

  const SERVER_ICONS: Record<ServerKey, any> = {
    jellyfin: JellyfinSVG,
    plex: PlexSVG,
  };

  const SERVER_LABELS: Record<ServerKey, string> = {
    jellyfin: "Jellyfin",
    plex: "Plex",
  };

  const SERVER_URL_PLACEHOLDERS: Record<ServerKey, string> = {
    jellyfin: "e.g. http://localhost:8096",
    plex: "e.g. http://localhost:32400",
  };

  const emptyState = (): MediaServerState => ({
    config: { enabled: false, baseUrl: "", apiKey: "", isMain: false },
    apiKeyIsSet: false,
    testing: false,
    saving: false,
  });

  let servers = $state<Record<ServerKey, MediaServerState>>({
    jellyfin: emptyState(),
    plex: emptyState(),
  });

  let loading = $state(false);
  let globalSaving = $state(false);
  let syncingMedia = $state(false);
  let confirmServerChange = $state(false);
  let syncBanner = $state<"resync" | "sync" | null>(null);

  // stores each server's enabled state from just before it was promoted to main,
  // so we can restore it if it gets demoted back to linked
  let enabledBeforePromotion = $state<Partial<Record<ServerKey, boolean>>>({});

  // track the saved main server key so we can warn when user changes it
  let savedMainServer = $state<ServerKey | null>(null);

  // the pending dropdown selection (may differ from saved)
  let pendingMain = $state<ServerKey | null>(null);

  // true when the user has selected a different main than what's saved
  const mainServerChanged = $derived(
    savedMainServer !== null &&
      pendingMain !== null &&
      pendingMain !== savedMainServer,
  );

  const mainServer = $derived<ServerKey | null>(pendingMain);
  const linkedServers = $derived<ServerKey[]>(
    SERVERS.filter((k) => k !== mainServer),
  );

  // handle changes from the config forms
  const handleConfigChange = (serverKey: ServerKey, event: CustomEvent) => {
    const { field, value } = event.detail;
    if (field === "enabled") servers[serverKey].config.enabled = value;
    else if (field === "baseUrl") servers[serverKey].config.baseUrl = value;
    else if (field === "apiKey") servers[serverKey].config.apiKey = value;
  };

  // test connection to a media server
  const testServer = async (serverKey: ServerKey) => {
    servers[serverKey].testing = true;
    const config = servers[serverKey].config;
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
      toast.success(
        `Connection test for ${SERVER_LABELS[serverKey]} was successful!`,
      );
    } catch (err: any) {
      toast.error(
        `Connection test for ${SERVER_LABELS[serverKey]} failed: ${err.message}`,
      );
    } finally {
      servers[serverKey].testing = false;
    }
  };

  // save a single server's settings (returns the sync_action from the API response)
  const saveServer = async (
    serverKey: ServerKey,
  ): Promise<"resync" | "sync" | null> => {
    servers[serverKey].saving = true;
    const config = { ...servers[serverKey].config };
    try {
      const response: {
        message: string;
        sync_action: "resync" | "sync" | null;
        data: {
          service_type: string;
          enabled: boolean;
          base_url: string;
          is_main: boolean;
        };
      } = await post_api("/api/settings/save/service", {
        service_type: serverKey,
        enabled: config.enabled,
        base_url: config.baseUrl,
        is_main: config.isMain,
        ...(config.apiKey ? { api_key: config.apiKey } : {}),
      });
      servers[serverKey].config = {
        enabled: response.data.enabled,
        baseUrl: response.data.base_url,
        apiKey: "",
        isMain: response.data.is_main,
      };
      servers[serverKey].apiKeyIsSet = true;
      if (response.data.is_main) {
        savedMainServer = serverKey;
        pendingMain = serverKey;
        for (const key of SERVERS) {
          if (key !== serverKey) {
            servers[key].config.isMain = false;
          }
        }
      }
      toast.success(response.message);
      return response.sync_action ?? null;
    } catch (err: any) {
      toast.error(
        `Error saving ${SERVER_LABELS[serverKey]} settings: ${err.message}`,
      );
      return null;
    } finally {
      servers[serverKey].saving = false;
    }
  };

  // track original config for change detection
  let originalConfigs = $state<Record<ServerKey, MediaServerConfig>>({
    jellyfin: { enabled: false, baseUrl: "", apiKey: "", isMain: false },
    plex: { enabled: false, baseUrl: "", apiKey: "", isMain: false },
  });

  // save all servers, prioritizing the main server first
  const saveAll = async () => {
    globalSaving = true;
    syncBanner = null;
    const mainServerKey = pendingMain; // capture before async ops
    try {
      const saveOrder: ServerKey[] = pendingMain
        ? [pendingMain, ...SERVERS.filter((k) => k !== pendingMain)]
        : [...SERVERS];
      for (const serverKey of saveOrder) {
        const config = servers[serverKey].config;
        const original = originalConfigs[serverKey];
        // only save if API key is present OR enabled/baseUrl changed
        const shouldSave =
          !!config.apiKey ||
          config.enabled !== original.enabled ||
          config.baseUrl !== original.baseUrl;
        if (config.baseUrl && shouldSave) {
          const action = await saveServer(serverKey);
          if (serverKey === mainServerKey && action) {
            syncBanner = action;
          }
        }
      }
    } finally {
      globalSaving = false;
    }
  };

  // trigger a media sync via the tasks API
  const syncMedia = async () => {
    syncingMedia = true;
    try {
      await post_api("/api/tasks/tasks/sync_media/run", {});
      toast.success(
        "Media sync started! Check the Tasks page to monitor progress.",
      );
      syncBanner = null;
    } catch (err: any) {
      toast.error(`Failed to start sync: ${err.message}`);
    } finally {
      syncingMedia = false;
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
            enabled: boolean;
            is_main: boolean | null;
            base_url: string;
            api_key: string;
          }
        >
      >("/api/settings/services");

      for (const serverKey of SERVERS) {
        const config = rawServices[serverKey];
        if (!config) continue;
        servers[serverKey].config = {
          enabled: config.enabled,
          baseUrl: config.base_url,
          apiKey: "",
          isMain: config.is_main ?? false,
        };
        // save original config for change detection
        originalConfigs[serverKey] = {
          enabled: config.enabled,
          baseUrl: config.base_url,
          apiKey: "",
          isMain: config.is_main ?? false,
        };
        servers[serverKey].apiKeyIsSet = !!config.api_key;
        if (config.is_main) {
          savedMainServer = serverKey;
          pendingMain = serverKey;
          // main server must always be enabled
          servers[serverKey].config.enabled = true;
        }
      }
    } catch (err: any) {
      toast.warning(`Error loading media server settings: ${err.message}`);
    } finally {
      loading = false;
    }
  };

  onMount(async () => {
    await loadSettings();
  });
</script>

{#if loading}
  <div class="flex items-center justify-center gap-3 text-muted-foreground p-8">
    <Spinner class="size-5" />
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
          Configure your media servers. One must be selected as the <strong
            >main server</strong
          >, which is the primary source for library and media data. Others will
          be
          <strong>linked</strong> for watch history and user data only.
        </p>
      </div>

      <hr />

      <!-- main Server -->
      <div>
        <h3 class="font-semibold text-foreground">Main Server</h3>
        <p class="text-sm text-muted-foreground mt-0.5">
          The primary source for library and media sync.
        </p>
      </div>

      <!-- dropdown to select main server -->
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
            value={pendingMain ?? undefined}
            onValueChange={(value) => {
              const newMain = value as ServerKey;
              const oldMain = pendingMain;

              if (oldMain && oldMain !== newMain) {
                // restore the demoted server's enabled state from before it was promoted
                servers[oldMain].config.enabled =
                  enabledBeforePromotion[oldMain] ?? false;
              }

              // save the incoming server's enabled state before forcing it on
              enabledBeforePromotion[newMain] = servers[newMain].config.enabled;

              pendingMain = newMain;
              for (const key of SERVERS) {
                servers[key].config.isMain = key === newMain;
              }
              // main server must always be enabled
              servers[newMain].config.enabled = true;
            }}
          >
            <Select.Trigger class="w-full cursor-pointer text-foreground">
              {#if pendingMain}
                {@const Icon = SERVER_ICONS[pendingMain]}
                <span class="flex items-center gap-2">
                  <Icon class="size-4 shrink-0" />
                  {SERVER_LABELS[pendingMain]}
                </span>
              {:else}
                <span class="text-muted-foreground">Select a server...</span>
              {/if}
            </Select.Trigger>
            <Select.Content>
              {#each SERVERS as serverKey}
                {@const Icon = SERVER_ICONS[serverKey]}
                <Select.Item value={serverKey} class="cursor-pointer">
                  <span class="flex items-center gap-2">
                    <Icon class="size-4 shrink-0" />
                    {SERVER_LABELS[serverKey]}
                  </span>
                </Select.Item>
              {/each}
            </Select.Content>
          </Select.Root>
        </div>
      </div>

      <!-- warning banner when changing main server -->
      {#if mainServerChanged}
        <div
          class="flex flex-col items-start gap-3 p-3 rounded-md bg-warning/50 border border-warning-secondary
            text-warning-foreground"
        >
          <div class="flex items-start gap-2">
            <AlertTriangle class="size-4 mt-0.5 shrink-0" />
            <p class="text-sm">
              Changing the main server from <strong
                >{SERVER_LABELS[savedMainServer!]}</strong
              >
              to
              <strong>{SERVER_LABELS[pendingMain!]}</strong> will trigger a full media
              resync. This may take a while. Make sure the new server is fully configured
              before saving.
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

      <!-- main server config form -->
      {#if mainServer}
        {@const apiKeyLabel =
          mainServer === SettingsTab.Plex ? "Token" : "API Key"}
        <div class="rounded-lg border border-border p-5 space-y-4">
          <ServiceConfigForm
            tabLabel={SERVER_LABELS[mainServer]}
            tabIcon={SERVER_ICONS[mainServer]}
            enabled={servers[mainServer].config.enabled}
            baseUrl={servers[mainServer].config.baseUrl}
            apiKey={servers[mainServer].config.apiKey}
            apiKeyIsSet={servers[mainServer].apiKeyIsSet}
            {apiKeyLabel}
            baseUrlPlaceholder={SERVER_URL_PLACEHOLDERS[mainServer]}
            disableToggle={true}
            onchange={(e) => handleConfigChange(mainServer, e)}
          />
          <div class="flex gap-2 justify-end">
            <TestButton
              onclick={() => testServer(mainServer)}
              disabled={servers[mainServer].testing ||
                servers[mainServer].saving ||
                globalSaving}
              loading={servers[mainServer].testing}
              size="sm">Test</TestButton
            >
          </div>
        </div>
      {/if}
    </section>

    <!-- linked servers -->
    {#if mainServer && linkedServers.length > 0}
      <hr />
      <section class="space-y-4">
        <div>
          <h2 class="font-semibold text-foreground">Linked Servers</h2>
          <p class="text-sm text-muted-foreground mt-0.5">
            Used for watch history and user data only. Libraries are sourced
            from the main server.
          </p>
        </div>

        <div class="space-y-4">
          {#each linkedServers as serverKey}
            {@const apiKeyLabel =
              serverKey === SettingsTab.Plex ? "Token" : "API Key"}
            <div class="rounded-lg border border-border p-5 space-y-4">
              <ServiceConfigForm
                tabLabel={SERVER_LABELS[serverKey]}
                tabIcon={SERVER_ICONS[serverKey]}
                enabled={servers[serverKey].config.enabled}
                baseUrl={servers[serverKey].config.baseUrl}
                apiKey={servers[serverKey].config.apiKey}
                apiKeyIsSet={servers[serverKey].apiKeyIsSet}
                {apiKeyLabel}
                baseUrlPlaceholder={SERVER_URL_PLACEHOLDERS[serverKey]}
                onchange={(e) => handleConfigChange(serverKey, e)}
              />
              <div class="flex gap-2 justify-end">
                <TestButton
                  onclick={() => testServer(serverKey)}
                  disabled={servers[serverKey].testing ||
                    servers[serverKey].saving ||
                    globalSaving}
                  class="cursor-pointer gap-2"
                  size="sm">Test</TestButton
                >
              </div>
            </div>
          {/each}
        </div>
      </section>
    {/if}

    <!-- save + sync banner -->
    {#if mainServer}
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
                Old media data is being replaced. Check the Tasks page to
                monitor progress.
              </p>
            {:else}
              <p class="text-sm font-medium">
                Settings saved - ready to import your media library.
              </p>
              {#if linkedServers.every((k) => !servers[k].apiKeyIsSet)}
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
