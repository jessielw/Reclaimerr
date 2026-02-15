<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import ServiceConfigForm from "$lib/components/settings/ServiceConfigForm.svelte";
  import Notifications from "$lib/components/settings/Notifications.svelte";
  import Tasks from "$lib/components/settings/tasks/Tasks.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import TestTube from "@lucide/svelte/icons/test-tube";
  import Save from "@lucide/svelte/icons/save";
  import Wrench from "@lucide/svelte/icons/wrench";
  import Bell from "@lucide/svelte/icons/bell";
  import CalendarClock from "@lucide/svelte/icons/calendar-clock";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
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
  import { auth } from "$lib/stores/auth";

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
    { id: ServiceType.Tasks, label: "Tasks", icon: CalendarClock },
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
    [ServiceType.Tasks]: {
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
    let errorOccurred = false;
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
      errorOccurred = true;
    } finally {
      savingService = false;
    }

    // if enabling Jellyfin or Plex, sync libraries after a short delay
    if (
      !errorOccurred &&
      config.enabled &&
      (serviceId === ServiceType.Jellyfin || serviceId === ServiceType.Plex)
    ) {
      setTimeout(async () => {
        await syncServiceLibraries(serviceId as ServiceType);
      }, 500);
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
              serviceType: serviceId as ServiceType,
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

  async function syncServiceLibraries(serviceId: ServiceType | null) {
    console.log("syncing libraries for", serviceId);
    try {
      loading = true;
      const response: Record<
        ServiceType,
        Array<{
          id: number;
          library_id: string;
          library_name: string;
          media_type: string;
          selected: boolean;
        }>
      > = await post_api("/api/settings/sync/libraries", {
        service_type: serviceId,
      });
      const updatedLibraries = response[serviceId as ServiceType];
      // reload settings to get updated libraries
      await loadServiceSettings();
      toast.success(
        `Successfully synced ${updatedLibraries.length} libraries!`,
      );
    } catch (err: any) {
      toast.error(`Error syncing libraries: ${err.message}`);
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
      <div
        class="flex p-8 items-center justify-center text-center gap-3 text-muted-foreground"
      >
        <Spinner class="size-5" />
        Loading settings...
      </div>
    {:else}
      <!-- service tabs -->
      <div class="bg-card rounded-lg border border-border p-6">
        <!-- mobile dropdown -->
        <div class="md:hidden mb-6">
          <label for="settings-tab-select" class="sr-only">
            Select Settings Tab
          </label>
          <div class="relative">
            <select
              id="settings-tab-select"
              bind:value={activeTab}
              class="w-full px-4 py-3 bg-background border border-input rounded-lg text-foreground
                     appearance-none cursor-pointer focus:outline-none focus:ring-2 focus:ring-ring
                     focus:border-transparent pr-10"
            >
              {#each tabs as tab}
                <option value={tab.id}>
                  {tab.label}
                </option>
              {/each}
            </select>
            <div
              class="pointer-events-none absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground"
            >
              <svg
                class="size-5"
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 20 20"
                fill="currentColor"
              >
                <path
                  fill-rule="evenodd"
                  d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 
                  01-1.414 0l-4-4a1 1 0 010-1.414z"
                  clip-rule="evenodd"
                />
              </svg>
            </div>
          </div>
        </div>

        <!-- desktop tabs -->
        <div class="hidden md:block border-b border-secondary mb-6">
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
              <!-- libraries -->
              <div class="flex items-center justify-between">
                <h2 class="text-xl font-semibold text-foreground">Libraries</h2>
                <!-- sync libraries -->
                <Button
                  class="cursor-pointer"
                  onclick={() => syncServiceLibraries(activeTab)}
                  ><RefreshCw /> Sync</Button
                >
              </div>
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

          <!-- action buttons for service tabs -->
          <div class="flex gap-3 justify-end">
            <Button
              onclick={() => testServiceConnection(activeTab)}
              disabled={testingService || savingService}
              class="cursor-pointer gap-2"
            >
              {#if testingService}
                <Spinner class="size-4" />
              {:else}
                <TestTube class="size-4" />
              {/if}
              Test
            </Button>
            <Button
              onclick={() => saveServiceSettings(activeTab)}
              disabled={savingService || testingService}
              class="cursor-pointer gap-2"
            >
              {#if savingService}
                <Spinner class="size-4" />
              {:else}
                <Save class="size-4" />
              {/if}
              Save
            </Button>
          </div>

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

          <!-- tasks -->
        {:else if activeTab === ServiceType.Tasks}
          <Tasks />

          <!-- notifications -->
        {:else if activeTab === ServiceType.Notifications}
          <Notifications userRole={$auth.user?.role || "user"} />
        {/if}
      </div>
    {/if}
  </div>
</div>
