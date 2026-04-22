<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import ServiceConfigForm from "$lib/components/settings/service-config-form.svelte";
  import MediaServers from "$lib/components/settings/media-servers.svelte";
  import Notifications from "$lib/components/settings/notifications.svelte";
  import Tasks from "$lib/components/settings/tasks/tasks.svelte";
  import BackgroundJobs from "$lib/components/settings/tasks/background-jobs.svelte";
  import Rules from "$lib/components/settings/rules/rules.svelte";
  import Account from "$lib/components/settings/account.svelte";
  import Users from "$lib/components/settings/users.svelte";
  import About from "$lib/components/settings/about.svelte";
  import General from "$lib/components/settings/general.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import TestTube from "@lucide/svelte/icons/test-tube";
  import Save from "@lucide/svelte/icons/save";
  import Wrench from "@lucide/svelte/icons/wrench";
  import Bell from "@lucide/svelte/icons/bell";
  import CalendarClock from "@lucide/svelte/icons/calendar-clock";
  import Boxes from "@lucide/svelte/icons/boxes";
  import Server from "@lucide/svelte/icons/server";
  import RadarrSVG from "$lib/components/svgs/RadarrSVG.svelte";
  import SonarrSVG from "$lib/components/svgs/SonarrSVG.svelte";
  import SeerrSVG from "$lib/components/svgs/SeerrSVG.svelte";
  import BookAlert from "@lucide/svelte/icons/book-alert";
  import UserCog from "@lucide/svelte/icons/user-cog";
  import Filter from "@lucide/svelte/icons/filter";
  import { toast } from "svelte-sonner";
  import { toTitleCase } from "$lib/utils/strings";
  import { SettingsTab, Permission, MEDIA_SERVERS } from "$lib/types/shared";
  import { auth } from "$lib/stores/auth";

  type Tab = {
    id: SettingsTab;
    label: string;
    icon: any;
    baseUrlPlaceholder?: string;
    adminOnly?: boolean;
  };

  type TabGroup = {
    label: string;
    tabs: Tab[];
    adminOnly?: boolean;
  };

  interface ServiceConfig {
    enabled: boolean;
    baseUrl: string;
    apiKey: string;
    extraSettings?: Record<string, string>;
  }

  type ServiceState = {
    config: ServiceConfig;
    apiKeyIsSet: boolean;
  };

  // non-media-server service tabs (jellyfin/plex managed by MediaServers component)
  const serviceTabs = [
    SettingsTab.Radarr,
    SettingsTab.Sonarr,
    SettingsTab.Seerr,
  ];

  // default extra settings for services (e.g. radarr/sonarr timeout) - these will be sent
  // to backend if the user changes them in the UI, but we need some defaults here to show
  // something in the form fields on initial load before the user changes them
  const DEFAULT_EXTRA_SETTINGS: Partial<Record<string, Record<string, any>>> = {
    [SettingsTab.Radarr]: { timeout: 300 },
    [SettingsTab.Sonarr]: { timeout: 300 },
  };

  // organize tabs into groups
  const tabGroups: TabGroup[] = [
    {
      label: "Services",
      adminOnly: true,
      tabs: [
        {
          id: SettingsTab.MediaServers,
          label: "Media Servers",
          icon: Server,
          adminOnly: true,
        },
        {
          id: SettingsTab.Radarr,
          label: "Radarr",
          icon: RadarrSVG,
          baseUrlPlaceholder: "e.g. http://localhost:7878",
          adminOnly: true,
        },
        {
          id: SettingsTab.Sonarr,
          label: "Sonarr",
          icon: SonarrSVG,
          baseUrlPlaceholder: "e.g. http://localhost:8989",
          adminOnly: true,
        },
        {
          id: SettingsTab.Seerr,
          label: "Seerr",
          icon: SeerrSVG,
          baseUrlPlaceholder: "e.g. http://localhost:5055",
          adminOnly: true,
        },
      ],
    },
    {
      label: "System",
      tabs: [
        {
          id: SettingsTab.General,
          label: "General",
          icon: Wrench,
          adminOnly: true,
        },
        {
          id: SettingsTab.Tasks,
          label: "Tasks",
          icon: CalendarClock,
          adminOnly: true,
        },
        {
          id: SettingsTab.BackgroundJobs,
          label: "Background Jobs",
          icon: Boxes,
          adminOnly: true,
        },
        { id: SettingsTab.Notifications, label: "Notifications", icon: Bell },
        { id: SettingsTab.Account, label: "Account", icon: UserCog },
        {
          id: SettingsTab.Rules,
          label: "Rules",
          icon: Filter,
          adminOnly: true,
        },
        {
          id: SettingsTab.Users,
          label: "Users",
          icon: UserCog,
          adminOnly: true,
        },
      ],
    },
    {
      label: "Info",
      tabs: [{ id: SettingsTab.About, label: "About", icon: BookAlert }],
    },
  ];

  // filter tabs based on user role
  const isAdmin = $derived($auth.user?.role === "admin");
  const canManageUsers = $derived(
    isAdmin || ($auth.user?.permissions ?? []).includes(Permission.ManageUsers),
  );
  const filteredTabGroups = $derived(
    tabGroups
      .filter((group) => !group.adminOnly || isAdmin)
      .map((group) => ({
        ...group,
        tabs: group.tabs.filter((tab) => {
          if (tab.id === SettingsTab.Users) {
            return canManageUsers;
          }
          return !tab.adminOnly || isAdmin;
        }),
      }))
      .filter((group) => group.tabs.length > 0),
  );

  // flatten for easier lookup
  const tabs: Tab[] = $derived(
    filteredTabGroups.flatMap((group) => group.tabs),
  );

  // states
  let loading = $state(false);
  let testingService = $state(false);
  let savingService = $state(false);

  // set initial active tab to first available tab for user
  let activeTab = $state(
    $auth.user?.role === "admin"
      ? SettingsTab.MediaServers
      : SettingsTab.Notifications,
  );

  const emptyServiceState = (serviceId?: string): ServiceState => ({
    config: {
      enabled: false,
      baseUrl: "",
      apiKey: "",
      extraSettings: serviceId ? (DEFAULT_EXTRA_SETTINGS[serviceId] ?? {}) : {},
    },
    apiKeyIsSet: false,
  });

  let serviceState = $state<Record<string, ServiceState>>({
    [SettingsTab.Radarr]: emptyServiceState(SettingsTab.Radarr),
    [SettingsTab.Sonarr]: emptyServiceState(SettingsTab.Sonarr),
    [SettingsTab.Seerr]: emptyServiceState(SettingsTab.Seerr),
  });

  // handler for service config changes (radarr/sonarr/seerr only)
  const handleServiceChange = (event: CustomEvent) => {
    const { field, value } = event.detail;
    if (field === "enabled") serviceState[activeTab].config.enabled = value;
    else if (field === "baseUrl")
      serviceState[activeTab].config.baseUrl = value;
    else if (field === "apiKey") serviceState[activeTab].config.apiKey = value;
    else if (field.startsWith("extraSettings.")) {
      const key = field.slice("extraSettings.".length);
      serviceState[activeTab].config.extraSettings = {
        ...(serviceState[activeTab].config.extraSettings ?? {}),
        [key]: value,
      };
    }
  };

  // helper to resolve extra settings with defaults (used when loading settings from backend,
  // if backend doesn't send extra_settings or sends empty object, we want to use our defined
  // defaults so that form fields aren't blank)
  const resolveExtraSettings = (
    extra_settings: Record<string, any> | undefined | null,
    serviceId: string,
  ): Record<string, any> => {
    return extra_settings && Object.keys(extra_settings).length > 0
      ? extra_settings
      : (DEFAULT_EXTRA_SETTINGS[serviceId] ?? {});
  };

  // test service connection
  const testServiceConnection = async (serviceId: string) => {
    testingService = true;
    const config = serviceState[serviceId as SettingsTab].config;
    try {
      // only send api_key if the user typed a new one (backend resolves existing key otherwise)
      const payload: Record<string, unknown> = {
        service_type: serviceId,
        enabled: config.enabled,
        base_url: config.baseUrl.replace(/\/+$/, ""),
      };
      if (config.apiKey) payload.api_key = config.apiKey;
      const response: boolean = await post_api(
        "/api/settings/test/service",
        payload,
      );
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
  };

  // save service settings (radarr/sonarr/seerr only)
  const saveServiceSettings = async (serviceId: string) => {
    savingService = true;
    const config = serviceState[serviceId as SettingsTab].config;
    try {
      const response: {
        message: string;
        data: {
          service_type: string;
          enabled: boolean;
          base_url: string;
          extra_settings?: Record<string, any>;
        };
      } = await post_api("/api/settings/save/service", {
        service_type: serviceId,
        base_url: config.baseUrl.replace(/\/+$/, ""),
        enabled: config.enabled,
        extra_settings: config.extraSettings,
        // only send api_key if the user typed a new one (backend resolves existing key otherwise)
        ...(config.apiKey ? { api_key: config.apiKey } : {}),
      });
      // update state: clear the typed key, keep baseUrl/enabled from response
      serviceState[serviceId as SettingsTab].config = {
        enabled: response.data.enabled,
        baseUrl: response.data.base_url,
        extraSettings: resolveExtraSettings(
          response.data.extra_settings,
          serviceId,
        ),
        apiKey: "",
      };
      serviceState[serviceId as SettingsTab].apiKeyIsSet = true;
      toast.success(response.message);
    } catch (err: any) {
      toast.error(
        `Error saving settings for ${toTitleCase(serviceId)}: ${err.message}`,
      );
    } finally {
      savingService = false;
    }
  };

  // load service settings (radarr/sonarr/seerr only - media servers handled by MediaServers component)
  const loadServiceSettings = async () => {
    try {
      loading = true;
      const rawServices = await get_api<
        Record<
          string,
          {
            enabled: boolean;
            base_url: string;
            api_key: string;
            extra_settings?: Record<string, any>;
          }
        >
      >("/api/settings/services");

      for (const [serviceId, config] of Object.entries(rawServices)) {
        if ((MEDIA_SERVERS as readonly string[]).includes(serviceId)) continue;
        serviceState[serviceId as SettingsTab].config = {
          enabled: config.enabled,
          baseUrl: config.base_url,
          apiKey: "",
          extraSettings: resolveExtraSettings(config.extra_settings, serviceId),
        };
        serviceState[serviceId as SettingsTab].apiKeyIsSet = !!config.api_key;
      }
    } catch (err: any) {
      toast.warning(`Error loading settings: ${err.message}`);
    } finally {
      loading = false;
    }
  };

  // helper to load icon from tabs
  const getTabIcon = (tabId: SettingsTab) => {
    const tab = tabs.find((t) => t.id === tabId);
    return tab ? tab.icon : null;
  };

  // load settings on mount
  onMount(() => {
    // only admins right now can adjust these settings
    if (isAdmin) loadServiceSettings();
  });
</script>

<div class="p-2.5 md:p-8">
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
      <div
        class="bg-card rounded-lg border border-border flex flex-col md:flex-row"
      >
        <!-- mobile: native grouped select -->
        <div class="md:hidden p-4 border-b border-border">
          <label for="settings-tab-select" class="sr-only"
            >Select Settings Tab</label
          >
          <div class="relative">
            <select
              id="settings-tab-select"
              bind:value={activeTab}
              class="w-full px-4 py-3 bg-background border border-input rounded-lg text-foreground
                     appearance-none cursor-pointer focus:outline-none focus:ring-2 focus:ring-ring
                     focus:border-transparent pr-10"
            >
              {#each filteredTabGroups as group}
                <optgroup label={group.label}>
                  {#each group.tabs as tab}
                    <option value={tab.id}>{tab.label}</option>
                  {/each}
                </optgroup>
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

        <!-- desktop: grouped left sidebar -->
        <nav
          class="hidden md:flex flex-col w-52 border-r border-border py-4 shrink-0"
        >
          {#each filteredTabGroups as group}
            <div class="mb-3 px-3">
              <p
                class="text-xs font-semibold uppercase tracking-wider text-muted-foreground/60 mb-1 px-2"
              >
                {group.label}
              </p>
              {#each group.tabs as tab}
                <button
                  class="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-md text-sm transition-colors
                    cursor-pointer
                    {activeTab === tab.id
                    ? 'bg-primary/10 text-primary font-medium'
                    : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'}"
                  onclick={() => (activeTab = tab.id)}
                >
                  <tab.icon class="size-4 shrink-0" />
                  {tab.label}
                </button>
              {/each}
            </div>
          {/each}
        </nav>

        <!-- content panel -->
        <div class="flex-1 p-6 min-w-0">
          <!-- settings -->
          {#if serviceTabs.includes(activeTab)}
            <ServiceConfigForm
              tabLabel={tabs.find((t) => t.id === activeTab)?.label || ""}
              tabIcon={getTabIcon(activeTab)}
              enabled={serviceState[activeTab].config.enabled}
              baseUrl={serviceState[activeTab].config.baseUrl}
              apiKey={serviceState[activeTab].config.apiKey}
              apiKeyIsSet={serviceState[activeTab].apiKeyIsSet}
              baseUrlPlaceholder={tabs.find((t) => t.id === activeTab)
                ?.baseUrlPlaceholder || "http://localhost:8096"}
              extraSettings={serviceState[activeTab].config.extraSettings ?? {}}
              onchange={handleServiceChange}
            />

            <!-- action buttons for service tabs (radarr/sonarr/seerr) -->
            <div class="flex gap-3 justify-end mt-3">
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

            <!-- media servers -->
          {:else if activeTab === SettingsTab.MediaServers}
            <MediaServers />

            <!-- general settings -->
          {:else if activeTab === SettingsTab.General}
            <General svgIcon={getTabIcon(activeTab)} />

            <!-- tasks -->
          {:else if activeTab === SettingsTab.Tasks}
            <Tasks svgIcon={getTabIcon(activeTab)} />

            <!-- background jobs -->
          {:else if activeTab === SettingsTab.BackgroundJobs}
            <BackgroundJobs svgIcon={getTabIcon(activeTab)} />

            <!-- notifications -->
          {:else if activeTab === SettingsTab.Notifications}
            <Notifications
              userRole={$auth.user?.role || "user"}
              svgIcon={getTabIcon(activeTab)}
            />

            <!-- account -->
          {:else if activeTab === SettingsTab.Account}
            <Account svgIcon={getTabIcon(activeTab)} />

            <!-- rules -->
          {:else if activeTab === SettingsTab.Rules}
            <Rules svgIcon={getTabIcon(activeTab)} />

            <!-- users -->
          {:else if activeTab === SettingsTab.Users}
            <Users svgIcon={getTabIcon(activeTab)} />

            <!-- about -->
          {:else if activeTab === SettingsTab.About}
            <About svgIcon={getTabIcon(activeTab)} />
          {/if}
        </div>
      </div>
    {/if}
  </div>
</div>
