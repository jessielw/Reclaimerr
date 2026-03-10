<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import ServiceConfigForm from "$lib/components/settings/service-config-form.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import JellyfinSVG from "$lib/components/svgs/JellyfinSVG.svelte";
  import PlexSVG from "$lib/components/svgs/PlexSVG.svelte";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import TestButton from "$lib/components/test-button.svelte";
  import Save from "@lucide/svelte/icons/save";
  import Tv from "@lucide/svelte/icons/tv";
  import Clapperboard from "@lucide/svelte/icons/clapperboard";
  import AlertTriangle from "@lucide/svelte/icons/triangle-alert";
  import { toast } from "svelte-sonner";
  import type { LibraryType } from "$lib/types/shared";
  import { MediaType, SettingsTab } from "$lib/types/shared";
  import * as Select from "$lib/components/ui/select/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import Checkbox from "$lib/components/ui/checkbox/checkbox.svelte";
  import { scrollIntoView } from "$lib/utils/misc";
  import InfoBox from "$lib/components/info-box.svelte";
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
    libraries: LibraryType[];
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
    libraries: [],
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
  let syncingLibraries = $state(false);
  let confirmServerChange = $state(false);

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

  // save a single server's settings
  const saveServer = async (serverKey: ServerKey) => {
    servers[serverKey].saving = true;
    const config = { ...servers[serverKey].config };
    const libraries = servers[serverKey].libraries;
    try {
      const response: {
        message: string;
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
        libraries:
          libraries.length > 0
            ? libraries.map((lib) => ({ id: lib.id, selected: lib.selected }))
            : undefined,
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
    } catch (err: any) {
      toast.error(
        `Error saving ${SERVER_LABELS[serverKey]} settings: ${err.message}`,
      );
    } finally {
      servers[serverKey].saving = false;
    }
  };

  // track original config for change detection
  let originalConfigs = $state<Record<ServerKey, MediaServerConfig>>({
    jellyfin: { enabled: false, baseUrl: "", apiKey: "", isMain: false },
    plex: { enabled: false, baseUrl: "", apiKey: "", isMain: false },
  });

  // save all servers, prioritizing the main server first since libraries depend on it
  const saveAll = async () => {
    globalSaving = true;
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
          await saveServer(serverKey);
        }
      }
      // auto-sync libraries on first save when none are loaded yet
      if (pendingMain && servers[pendingMain].libraries.length === 0) {
        await syncLibraries(pendingMain);
      }
    } finally {
      globalSaving = false;
    }
  };

  // sync libraries from the main server
  const syncLibraries = async (serverKey: ServerKey) => {
    syncingLibraries = true;
    try {
      await post_api("/api/settings/sync/libraries", {
        service_type: serverKey,
      });
      // refresh only libraries without a full-page reload
      const rawServices = await get_api<
        Record<
          string,
          {
            enabled: boolean;
            is_main: boolean | null;
            base_url: string;
            api_key: string;
            libraries?: Array<{
              id: number;
              library_id: string;
              library_name: string;
              media_type: string;
              selected: boolean;
            }> | null;
          }
        >
      >("/api/settings/services");
      const config = rawServices[serverKey];
      if (config?.libraries) {
        servers[serverKey].libraries = config.libraries.map((lib) => ({
          id: lib.id,
          libraryId: lib.library_id,
          libraryName: lib.library_name,
          mediaType:
            lib.media_type === "movie" ? MediaType.Movie : MediaType.Series,
          serviceType: serverKey as any,
          selected: lib.selected,
        }));
      }
      if (servers[serverKey].libraries.length === 0) {
        toast.warning(
          `No libraries found from ${SERVER_LABELS[serverKey]}. Make sure it's configured correctly.`,
        );
      } else {
        toast.success(
          `Successfully synced ${servers[serverKey].libraries.length} libraries from ${SERVER_LABELS[serverKey]}!`,
        );
      }
    } catch (err: any) {
      toast.error(`Error syncing libraries: ${err.message}`);
    } finally {
      syncingLibraries = false;
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
            libraries?: Array<{
              id: number;
              library_id: string;
              library_name: string;
              media_type: string;
              selected: boolean;
            }> | null;
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
        servers[serverKey].libraries = config.libraries
          ? config.libraries.map((lib) => ({
              id: lib.id,
              libraryId: lib.library_id,
              libraryName: lib.library_name,
              mediaType:
                lib.media_type === "movie" ? MediaType.Movie : MediaType.Series,
              serviceType: serverKey as any,
              selected: lib.selected,
            }))
          : [];
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
    <!-- main Server -->
    <section class="space-y-4">
      <div>
        <h2 class="text-lg font-semibold text-foreground">Main Server</h2>
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

      <!-- main server config form + libraries -->
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

        <!-- libraries -->
        <div class="rounded-lg border border-border p-5 space-y-4">
          <div class="flex items-start justify-between gap-4">
            <div>
              <h2 class="text-lg font-semibold text-foreground">Libraries</h2>
              <p class="text-sm text-muted-foreground mt-0.5">
                Select which libraries from <strong
                  >{SERVER_LABELS[mainServer]}</strong
                > to include in media sync.
              </p>
            </div>
            <Button
              size="sm"
              class="cursor-pointer shrink-0 gap-2"
              disabled={syncingLibraries || globalSaving}
              onclick={() => syncLibraries(mainServer)}
            >
              {#if syncingLibraries}
                <Spinner class="size-4" />
              {:else}
                <RefreshCw class="size-4" />
              {/if}
              Sync
            </Button>
          </div>

          {#if servers[mainServer].libraries.length > 0}
            <div class="flex flex-wrap gap-1">
              {#each servers[mainServer].libraries as library}
                <Badge
                  variant="secondary"
                  class="text-sm px-3 py-1 rounded-full bg-muted text-muted-foreground m-0.5 w-55 justify-between
                    {library.mediaType === MediaType.Movie
                    ? 'border-movie'
                    : 'border-series'}"
                >
                  <div class="inline-flex flex-1 max-w-4/5 items-center gap-2">
                    {#if library.mediaType === MediaType.Movie}
                      <Clapperboard size="18" />
                    {:else}
                      <Tv size="18" />
                    {/if}
                    <span class="truncate" title={library.libraryName}>
                      {library.libraryName}
                    </span>
                  </div>
                  <Switch
                    class="ml-1 cursor-pointer"
                    checked={library.selected}
                    onCheckedChange={(checked) => {
                      library.selected = checked;
                    }}
                  />
                </Badge>
              {/each}
            </div>
          {:else}
            <InfoBox title="No Libraries Loaded">
              <p>
                No libraries loaded yet. <a
                  href="#save-all-button"
                  class="text-primary underline cursor-pointer"
                  onclick={(e) => scrollIntoView(e, true)}>Save</a
                >
                your settings and they'll sync automatically, or click Sync to load
                them now.
              </p>
            </InfoBox>
          {/if}
        </div>
      {/if}
    </section>

    <!-- linked servers -->
    {#if mainServer && linkedServers.length > 0}
      <section class="space-y-4">
        <div>
          <h2 class="text-lg font-semibold text-foreground">Linked Servers</h2>
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

    <!-- save all button -->
    {#if mainServer}
      <div class="flex justify-end pt-4 border-t border-border">
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
            <Save class="size-4" />
            Save All Changes
          {/if}
        </Button>
      </div>
    {/if}
  </div>
{/if}
