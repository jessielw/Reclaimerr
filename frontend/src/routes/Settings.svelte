<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import ServiceConfigForm from "$lib/components/ServiceConfigForm.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import TestTube from "@lucide/svelte/icons/test-tube";
  import Save from "@lucide/svelte/icons/save";
  import Wrench from "@lucide/svelte/icons/wrench";
  import Bell from "@lucide/svelte/icons/bell";
  import JellyfinSVG from "$lib/components/svgs/JellyfinSVG.svelte";
  import PlexSVG from "$lib/components/svgs/PlexSVG.svelte";
  import RadarrSVG from "$lib/components/svgs/RadarrSVG.svelte";
  import SonarrSVG from "$lib/components/svgs/SonarrSVG.svelte";
  import SeerrSVG from "$lib/components/svgs/SeerrSVG.svelte";
  import Tv from "@lucide/svelte/icons/tv";
  import Clapperboard from "@lucide/svelte/icons/clapperboard";
  import { toast } from "svelte-sonner";
  import { toTitleCase } from "$lib/utils/strings";
  import type { LibraryType } from "$lib/types/shared";
  import { MediaType, ServiceType } from "$lib/types/shared";

  interface ServiceConfig {
    enabled: boolean;
    baseUrl: string;
    apiKey: string;
  }

  type ServiceState = {
    config: ServiceConfig;
    libraries: LibraryType[];
  };

  // services
  const serviceTabs = [
    ServiceType.Jellyfin,
    ServiceType.Plex,
    ServiceType.Radarr,
    ServiceType.Sonarr,
    ServiceType.Seerr,
  ];

  const tabs = [
    {
      id: ServiceType.Jellyfin,
      label: "Jellyfin",
      icon: JellyfinSVG,
      baseUrlPlaceholder: "e.g. http://localhost:8096",
    },
    {
      id: ServiceType.Plex,
      label: "Plex",
      icon: PlexSVG,
      baseUrlPlaceholder: "e.g. http://localhost:32400",
    },
    {
      id: ServiceType.Radarr,
      label: "Radarr",
      icon: RadarrSVG,
      baseUrlPlaceholder: "e.g. http://localhost:7878",
    },
    {
      id: ServiceType.Sonarr,
      label: "Sonarr",
      icon: SonarrSVG,
      baseUrlPlaceholder: "e.g. http://localhost:8989",
    },
    {
      id: ServiceType.Seerr,
      label: "Seerr",
      icon: SeerrSVG,
      baseUrlPlaceholder: "e.g. http://localhost:5055",
    },
    { id: ServiceType.General, label: "General", icon: Wrench },
    { id: ServiceType.Notifications, label: "Notifications", icon: Bell },
  ];

  // states
  let loading = $state(false);
  let activeTab = $state(ServiceType.Jellyfin);
  let testingService = $state(false);
  let savingService = $state(false);

  // unified state for each service even though
  // Jellyfin & Plex will be the only ones with libraries
  let serviceState = $state<Record<ServiceType, ServiceState>>({
    [ServiceType.Jellyfin]: {
      config: { enabled: false, baseUrl: "", apiKey: "" },
      libraries: [],
    },
    [ServiceType.Plex]: {
      config: { enabled: false, baseUrl: "", apiKey: "" },
      libraries: [],
    },
    [ServiceType.Radarr]: {
      config: { enabled: false, baseUrl: "", apiKey: "" },
      libraries: [],
    },
    [ServiceType.Sonarr]: {
      config: { enabled: false, baseUrl: "", apiKey: "" },
      libraries: [],
    },
    [ServiceType.Seerr]: {
      config: { enabled: false, baseUrl: "", apiKey: "" },
      libraries: [],
    },
    [ServiceType.General]: {
      config: { enabled: false, baseUrl: "", apiKey: "" },
      libraries: [],
    },
    [ServiceType.Notifications]: {
      config: { enabled: false, baseUrl: "", apiKey: "" },
      libraries: [],
    },
  });

  // handler for service config changes
  function handleServiceChange(event: CustomEvent) {
    const { field, value } = event.detail;
    if (field === "enabled") serviceState[activeTab].config.enabled = value;
    else if (field === "baseUrl")
      serviceState[activeTab].config.baseUrl = value;
    else if (field === "apiKey") serviceState[activeTab].config.apiKey = value;
  }

  // test service connection
  async function testServiceConnection(serviceId: string) {
    testingService = true;
    const config = serviceState[serviceId as ServiceType].config;
    try {
      const response: boolean = await post_api("/api/settings/test/service", {
        service_type: serviceId,
        enabled: config.enabled,
        base_url: config.baseUrl,
        api_key: config.apiKey,
      });
      if (!response) {
        throw new Error("Connection test failed");
      }
      toast.success(
        `Connection test for ${toTitleCase(serviceId)} was successful!`,
      );
    } catch (err: any) {
      toast.error(
        `Connection test for ${toTitleCase(serviceId)} failed: ${err.message}`,
      );
    } finally {
      testingService = false;
    }
  }

  // save service settings
  async function saveServiceSettings(serviceId: string) {
    savingService = true;
    const config = serviceState[serviceId as ServiceType].config;
    const libraries = serviceState[serviceId as ServiceType].libraries;
    try {
      const response: {
        message: string;
        data: {
          service_type: string;
          enabled: boolean;
          base_url: string;
          api_key: string;
        };
      } = await post_api("/api/settings/save/service", {
        service_type: serviceId,
        enabled: config.enabled,
        base_url: config.baseUrl,
        api_key: config.apiKey,
        libraries:
          libraries.length > 0
            ? libraries.map((lib) => ({
                id: lib.id,
                selected: lib.selected,
              }))
            : undefined,
      });
      // update state based on response
      serviceState[serviceId as ServiceType].config = {
        enabled: response.data.enabled,
        baseUrl: response.data.base_url,
        apiKey: response.data.api_key,
      };
      toast.success(response.message);
    } catch (err: any) {
      toast.error(
        `Error saving settings for ${toTitleCase(serviceId)}: ${err.message}`,
      );
    } finally {
      savingService = false;
    }
  }

  // load service settings
  async function loadServiceSettings() {
    try {
      loading = true;
      // fetch service settings
      const rawServices = await get_api<
        Record<
          string,
          {
            enabled: boolean;
            base_url: string;
            api_key: string;
            libraries?: Array<{
              id: number;
              library_id: string;
              library_name: string;
              media_type: string;
              selected: boolean;
            }>;
          }
        >
      >("/api/settings/services");

      // loop and map to our services state
      for (const [serviceId, config] of Object.entries(rawServices)) {
        serviceState[serviceId as ServiceType].config = {
          enabled: config.enabled,
          baseUrl: config.base_url, // base_url -> baseUrl
          apiKey: config.api_key, // api_key -> apiKey
        };
        // if plex or jellyfin, load libraries
        if (
          serviceId === ServiceType.Jellyfin ||
          serviceId === ServiceType.Plex
        ) {
          const rawLibraries = config.libraries || [];
          serviceState[serviceId as ServiceType].libraries = rawLibraries.map(
            (lib) => ({
              id: lib.id,
              libraryId: lib.library_id,
              libraryName: lib.library_name,
              mediaType:
                lib.media_type === "movie" ? MediaType.Movie : MediaType.Series,
              selected: lib.selected,
            }),
          );
        }
      }
    } catch (err: any) {
      toast.warning(`Error loading settings: ${err.message}`);
    } finally {
      loading = false;
    }
  }

  // load settings on mount
  onMount(() => {
    loadServiceSettings();
  });
</script>

<div class="p-8">
  <div class="max-w-7xl mx-auto">
    <div class="mb-2">
      <h1 class="text-3xl font-bold text-foreground mb-2">Settings</h1>
    </div>

    {#if loading}
      <div class="p-8 text-center text-muted-foreground">
        <Spinner size="lg" class="text-primary" />
        <p class="mt-4">Loading settings...</p>
      </div>
    {:else}
      <!-- service tabs -->
      <div class="bg-card rounded-lg border border-border p-6">
        <!-- buttons -->
        <div class="border-b border-secondary mb-6">
          <div class="flex gap-2 overflow-x-auto">
            {#each tabs as tab}
              <Button
                variant="ghost"
                class="cursor-pointer bg-transparent text-foreground {activeTab ===
                tab.id
                  ? 'border-b-4 border-primary rounded-none'
                  : ''}"
                onclick={() => (activeTab = tab.id)}
              >
                <span class="mr-2"><tab.icon /></span>
                {tab.label}
              </Button>
            {/each}
          </div>
        </div>

        <!-- settings -->
        {#if serviceTabs.includes(activeTab)}
          <ServiceConfigForm
            tabLabel={tabs.find((t) => t.id === activeTab)?.label || ""}
            enabled={serviceState[activeTab].config.enabled}
            baseUrl={serviceState[activeTab].config.baseUrl}
            apiKey={serviceState[activeTab].config.apiKey}
            baseUrlPlaceholder={tabs.find((t) => t.id === activeTab)
              ?.baseUrlPlaceholder || "http://localhost:8096"}
            onchange={handleServiceChange}
          />

          <!-- if plex or jellyfin add additional settings -->
          {#if activeTab === ServiceType.Jellyfin || activeTab === ServiceType.Plex}
            <hr class="h-1 my-4 border-muted-foreground" />

            <div class="flex flex-col justify-between mb-4">
              <h2 class="text-xl font-semibold text-foreground">Libraries</h2>
              <p class="mt-1 text-xs text-muted-foreground">
                Select which libraries to manage
              </p>
            </div>

            {#if activeTab === ServiceType.Jellyfin || activeTab === ServiceType.Plex}
              {#if serviceState[activeTab].libraries.length > 0}
                {#each serviceState[activeTab].libraries as library}
                  <Badge
                    variant="secondary"
                    class="text-sm px-3 py-1 rounded-full bg-muted text-muted-foreground m-0.5 w-55 justify-between
                      {library.mediaType === MediaType.Movie
                      ? 'border-movie'
                      : 'border-series'}"
                  >
                    <div
                      class="inline-flex flex-1 max-w-4/5 items-center gap-2"
                    >
                      {#if library.mediaType === MediaType.Movie}<Clapperboard
                          size="18"
                        />{:else}<Tv size="18" />{/if}
                      <span class="truncate" title={library.libraryName}
                        >{library.libraryName}</span
                      >
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
              {/if}
            {/if}

            <hr class="h-1 my-4 border-muted-foreground" />
          {/if}

          <!-- general settings #TODO: we'll implement this eventually... -->
        {:else if activeTab === ServiceType.General}
          <!-- <div class="bg-gray-900 rounded-lg border border-gray-800 p-6 mt-6">
            <h2 class="text-xl font-semibold text-gray-100 mb-4">Additional Settings</h2>
            
            <div class="space-y-6">
              <div>
                <h3 class="text-sm font-medium text-gray-300 mb-3">Library Management Priority</h3>
                <p class="text-xs text-gray-500 mb-3">
                  When multiple services are configured, priority determines which service manages deletions
                </p>
                <div class="space-y-2 text-sm text-gray-400">
                  <div class="flex items-center gap-2">
                    <span class="text-primary-500 font-medium">1.</span>
                    <span>Radarr/Sonarr (if configured)</span>
                  </div>
                  <div class="flex items-center gap-2">
                    <span class="text-primary-500 font-medium">2.</span>
                    <span>Plex or Jellyfin</span>
                  </div>
                </div>
              </div>

              <div>
                <label class="block text-sm font-medium text-gray-300 mb-2">Shared Library Detection</label>
                <select class="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent">
                  <option>Auto-detect shared libraries</option>
                  <option>Prefer Plex for deletions</option>
                  <option>Prefer Jellyfin for deletions</option>
                </select>
                <p class="text-xs text-gray-500 mt-1">
                  If both Plex and Jellyfin share the same media library, specify which to use for cleanup
                </p>
              </div>

              <div>
                <label class="flex items-center gap-3 cursor-pointer">
                  <input type="checkbox" class="w-5 h-5 rounded bg-gray-800 border-gray-700" />
                  <div>
                    <span class="text-gray-300">Reset Seerr requests on deletion</span>
                    <p class="text-xs text-gray-500 mt-1">
                      Automatically reset user requests in Seerr when media is deleted
                    </p>
                  </div>
                </label>
              </div>
            </div>
          </div> -->

          <!-- notifications #TODO -->
        {:else if activeTab === ServiceType.Notifications}
          <p class="text-gray-400">Notification settings will go here.</p>
        {/if}

        <!-- button box -->
        <div class="flex gap-3 pt-4 justify-end">
          {#if serviceTabs.includes(activeTab)}
            <Button
              size="icon"
              class="cursor-pointer"
              onclick={() => testServiceConnection(activeTab)}
              disabled={testingService || savingService}
            >
              {#if testingService}
                <Spinner class="size-3/5" />
              {:else}
                <TestTube class="size-3/5" />
              {/if}
            </Button>
          {/if}
          <Button
            size="icon"
            class="cursor-pointer"
            onclick={() => saveServiceSettings(activeTab)}
            disabled={savingService || testingService}
          >
            {#if savingService}
              <Spinner class="size-3/5" />
            {:else}
              <Save class="size-3/5" />
            {/if}
          </Button>
        </div>
      </div>
    {/if}
  </div>
</div>
