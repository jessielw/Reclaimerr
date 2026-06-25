<script lang="ts">
  import { onMount } from "svelte";
  import { delete_api, get_api, post_api } from "$lib/api";
  import ServiceConfigForm from "$lib/components/settings/service-config-form.svelte";
  import MetadataProviderStatusPanel from "$lib/components/settings/metadata-provider-status-panel.svelte";
  import InstanceManagerBar from "$lib/components/settings/instance-manager-bar.svelte";
  import MediaServers from "$lib/components/settings/media-servers.svelte";
  import Notifications from "$lib/components/settings/notifications.svelte";
  import Tasks from "$lib/components/settings/tasks/tasks.svelte";
  import Account from "$lib/components/settings/account.svelte";
  import Users from "$lib/components/settings/users.svelte";
  import About from "$lib/components/settings/about.svelte";
  import General from "$lib/components/settings/general.svelte";
  import Authentication from "$lib/components/settings/authentication.svelte";
  import UserSignals from "$lib/components/settings/user-signals.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import TestButton from "$lib/components/test-button.svelte";
  import * as Select from "$lib/components/ui/select";
  import Save from "@lucide/svelte/icons/save";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import Plus from "@lucide/svelte/icons/plus";
  import Wrench from "@lucide/svelte/icons/wrench";
  import Bell from "@lucide/svelte/icons/bell";
  import CalendarClock from "@lucide/svelte/icons/calendar-clock";
  import Server from "@lucide/svelte/icons/server";
  import RadarrSVG from "$lib/components/svgs/radarr-svg.svelte";
  import SonarrSVG from "$lib/components/svgs/sonarr-svg.svelte";
  import SeerrSVG from "$lib/components/svgs/seerr-svg.svelte";
  import TautulliSVG from "$lib/components/svgs/tautulli-svg.svelte";
  import BookAlert from "@lucide/svelte/icons/book-alert";
  import UserCog from "@lucide/svelte/icons/user-cog";
  import KeyRound from "@lucide/svelte/icons/key-round";
  import { toast } from "svelte-sonner";
  import { toTitleCase } from "$lib/utils/strings";
  import {
    SettingsTab,
    Permission,
    MEDIA_SERVERS,
    type MetadataProviderStatusResponse,
  } from "$lib/types/shared";
  import { auth } from "$lib/stores/auth";

  type Tab = {
    id: SettingsTab;
    label: string;
    icon: any;
    baseUrlPlaceholder?: string;
    adminOnly?: boolean;
    lockName?: boolean;
    hideBaseUrl?: boolean;
  };

  type TabGroup = {
    label: string;
    tabs: Tab[];
    adminOnly?: boolean;
  };

  interface ServiceConfig {
    id?: number | null;
    name?: string;
    enabled: boolean;
    baseUrl: string;
    apiKey: string;
    extraSettings?: Record<string, any>;
  }

  type ServiceState = {
    config: ServiceConfig;
    apiKeyIsSet: boolean;
  };

  type ConfirmIntent =
    | { kind: "switch-instance"; serviceId: string; targetId: number | null }
    | { kind: "new-instance"; serviceId: string }
    | { kind: "delete-instance"; serviceId: string };

  // Non-media-server configuration tabs. Media servers are managed by MediaServers.
  const serviceTabs = [
    SettingsTab.Radarr,
    SettingsTab.Sonarr,
    SettingsTab.Seerr,
    SettingsTab.Tautulli,
    SettingsTab.MDBList,
    SettingsTab.OMDb,
  ];
  const metadataProviderTabs = [SettingsTab.MDBList, SettingsTab.OMDb];

  const DEFAULT_SERVICE_NAMES: Partial<Record<string, string>> = {
    [SettingsTab.MDBList]: "MDBList",
    [SettingsTab.OMDb]: "OMDb",
  };

  const DEFAULT_SERVICE_BASE_URLS: Partial<Record<string, string>> = {
    [SettingsTab.MDBList]: "https://api.mdblist.com",
    [SettingsTab.OMDb]: "https://www.omdbapi.com",
  };

  const serviceDisplayName = (serviceId: string) =>
    DEFAULT_SERVICE_NAMES[serviceId] ?? toTitleCase(serviceId);

  const serviceBaseUrl = (serviceId: string, value?: string | null) => {
    const baseUrl = (value ?? "").trim();
    return baseUrl || DEFAULT_SERVICE_BASE_URLS[serviceId] || "";
  };

  const isMetadataProviderTab = (serviceId: string) =>
    metadataProviderTabs.includes(serviceId as SettingsTab);

  // Default extra settings are sent when saved and keep initial form fields populated.
  const DEFAULT_EXTRA_SETTINGS: Partial<Record<string, Record<string, any>>> = {
    [SettingsTab.Radarr]: { timeout: 300 },
    [SettingsTab.Sonarr]: { timeout: 300 },
    [SettingsTab.MDBList]: {
      request_limit: 950,
      supporter_mode: false,
      request_delay_seconds: 1.0,
    },
    [SettingsTab.OMDb]: { request_limit: 950 },
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
          lockName: true,
        },
        {
          id: SettingsTab.Tautulli,
          label: "Tautulli",
          icon: TautulliSVG,
          baseUrlPlaceholder: "e.g. http://localhost:8181",
          adminOnly: true,
          lockName: true,
        },
      ],
    },
    {
      label: "Metadata Providers",
      adminOnly: true,
      tabs: [
        {
          id: SettingsTab.MDBList,
          label: "MDBList",
          icon: KeyRound,
          baseUrlPlaceholder: "https://api.mdblist.com",
          adminOnly: true,
          lockName: true,
          hideBaseUrl: true,
        },
        {
          id: SettingsTab.OMDb,
          label: "OMDb",
          icon: KeyRound,
          baseUrlPlaceholder: "https://www.omdbapi.com",
          adminOnly: true,
          lockName: true,
          hideBaseUrl: true,
        },
      ],
    },
    {
      label: "System",
      tabs: [
        { id: SettingsTab.Account, label: "Account", icon: UserCog },
        {
          id: SettingsTab.Authentication,
          label: "Authentication",
          icon: KeyRound,
          adminOnly: true,
        },
        {
          id: SettingsTab.General,
          label: "General",
          icon: Wrench,
          adminOnly: true,
        },
        { id: SettingsTab.Notifications, label: "Notifications", icon: Bell },
        {
          id: SettingsTab.Tasks,
          label: "Tasks",
          icon: CalendarClock,
          adminOnly: true,
        },
        {
          id: SettingsTab.Users,
          label: "Users",
          icon: UserCog,
          adminOnly: true,
        },
        {
          id: SettingsTab.UserSignals,
          label: "User Signals",
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
  let metadataProviderStatus = $state<MetadataProviderStatusResponse | null>(
    null,
  );
  let metadataProviderStatusLoading = $state(false);
  let metadataProviderStatusError = $state<string | null>(null);
  type TestStatus = "idle" | "loading" | "success" | "error";
  let serviceTestStatus = $state<Record<SettingsTab, TestStatus>>({
    [SettingsTab.Radarr]: "idle",
    [SettingsTab.Sonarr]: "idle",
    [SettingsTab.Seerr]: "idle",
    [SettingsTab.Tautulli]: "idle",
    [SettingsTab.MDBList]: "idle",
    [SettingsTab.OMDb]: "idle",
    [SettingsTab.MediaServers]: "idle",
    [SettingsTab.Jellyfin]: "idle",
    [SettingsTab.Emby]: "idle",
    [SettingsTab.Plex]: "idle",
    [SettingsTab.General]: "idle",
    [SettingsTab.Authentication]: "idle",
    [SettingsTab.UserSignals]: "idle",
    [SettingsTab.Tasks]: "idle",
    [SettingsTab.Notifications]: "idle",
    [SettingsTab.Account]: "idle",
    [SettingsTab.Rules]: "idle",
    [SettingsTab.Users]: "idle",
    [SettingsTab.About]: "idle",
  });

  // set initial active tab to first available tab for user
  let activeTab = $state(
    $auth.user?.role === "admin"
      ? SettingsTab.MediaServers
      : SettingsTab.Notifications,
  );

  // empty state for a service
  const emptyServiceState = (serviceId?: string): ServiceState => ({
    config: {
      enabled: true,
      name: serviceId ? serviceDisplayName(serviceId) : "",
      baseUrl: serviceId ? (DEFAULT_SERVICE_BASE_URLS[serviceId] ?? "") : "",
      apiKey: "",
      extraSettings: serviceId ? (DEFAULT_EXTRA_SETTINGS[serviceId] ?? {}) : {},
    },
    apiKeyIsSet: false,
  });

  let serviceState = $state<Record<string, ServiceState>>({
    [SettingsTab.Radarr]: emptyServiceState(SettingsTab.Radarr),
    [SettingsTab.Sonarr]: emptyServiceState(SettingsTab.Sonarr),
    [SettingsTab.Seerr]: emptyServiceState(SettingsTab.Seerr),
    [SettingsTab.Tautulli]: emptyServiceState(SettingsTab.Tautulli),
    [SettingsTab.MDBList]: emptyServiceState(SettingsTab.MDBList),
    [SettingsTab.OMDb]: emptyServiceState(SettingsTab.OMDb),
  });
  let arrInstances = $state<Record<string, ServiceConfig[]>>({
    [SettingsTab.Radarr]: [],
    [SettingsTab.Sonarr]: [],
  });
  let showConfirmDialog = $state(false);
  let confirmTitle = $state("");
  let confirmDescription = $state("");
  let confirmActionLabel = $state("Confirm");
  let confirmActionClass = $state("cursor-pointer hover");
  let confirmIntent = $state<ConfirmIntent | null>(null);

  // helper to get instance name for select trigger
  const getArrInstanceName = (serviceId: string, id: number | null) => {
    if (id === null) return null;
    const instance = (arrInstances[serviceId] ?? []).find(
      (item) => item.id === id,
    );
    if (!instance) return null;
    return `${instance.name}${instance.enabled ? "" : " (disabled)"}`;
  };

  // helper to check if current config has unsaved changes compared to loaded instances
  const hasDirtyConfig = (serviceId: string) => {
    const current = serviceState[serviceId as SettingsTab].config;
    const saved = (arrInstances[serviceId] ?? []).find(
      (x) => x.id === current.id,
    );
    if (!saved) {
      return !!(
        current.baseUrl.trim() ||
        current.apiKey.trim() ||
        (current.name ?? "").trim() !== serviceDisplayName(serviceId) ||
        current.enabled
      );
    }
    return (
      current.name !== saved.name ||
      current.enabled !== saved.enabled ||
      current.baseUrl !== saved.baseUrl ||
      JSON.stringify(current.extraSettings ?? {}) !==
        JSON.stringify(saved.extraSettings ?? {}) ||
      !!current.apiKey.trim()
    );
  };

  // confirm dialog handlers
  const openConfirmDialog = (
    intent: ConfirmIntent,
    options: {
      title: string;
      description: string;
      actionLabel: string;
      destructive?: boolean;
    },
  ) => {
    confirmIntent = intent;
    confirmTitle = options.title;
    confirmDescription = options.description;
    confirmActionLabel = options.actionLabel;
    confirmActionClass = options.destructive
      ? "cursor-pointer hover bg-destructive text-destructive-foreground hover:bg-destructive/90"
      : "cursor-pointer hover";
    showConfirmDialog = true;
  };

  const closeConfirmDialog = () => {
    showConfirmDialog = false;
    confirmIntent = null;
  };

  // handlers for arr instance selection/creation/deletion with confirm dialog if dirty config exists
  const applyArrInstanceSelection = (serviceId: string, id: number | null) => {
    const instance = arrInstances[serviceId]?.find((item) => item.id === id);
    if (!instance) return;
    serviceTestStatus[serviceId as SettingsTab] = "idle";
    serviceState[serviceId as SettingsTab].config = {
      ...instance,
      apiKey: "",
      extraSettings: resolveExtraSettings(instance.extraSettings, serviceId),
    };
    serviceState[serviceId as SettingsTab].apiKeyIsSet = true;
  };

  // create new instance draft with confirm if dirty
  const createNewArrInstanceDraft = (serviceId: string) => {
    serviceTestStatus[serviceId as SettingsTab] = "idle";
    serviceState[serviceId as SettingsTab] = emptyServiceState(serviceId);
  };

  // instance selection with confirm if dirty
  const selectArrInstance = (serviceId: string, id: number | null) => {
    if (hasDirtyConfig(serviceId)) {
      openConfirmDialog(
        { kind: "switch-instance", serviceId, targetId: id },
        {
          title: "Discard Unsaved Changes?",
          description:
            "You have unsaved changes. Switching instances will discard them.",
          actionLabel: "Discard and Switch",
        },
      );
      return;
    }
    applyArrInstanceSelection(serviceId, id);
  };

  // delete instance with confirm
  const newArrInstance = (serviceId: string) => {
    if (hasDirtyConfig(serviceId)) {
      openConfirmDialog(
        { kind: "new-instance", serviceId },
        {
          title: "Discard Unsaved Changes?",
          description:
            "You have unsaved changes. Creating a new draft will discard them.",
          actionLabel: "Discard and Create",
        },
      );
      return;
    }
    createNewArrInstanceDraft(serviceId);
  };

  // perform instance deletion
  const performDeleteArrInstance = async (serviceId: string) => {
    const current = serviceState[serviceId as SettingsTab].config;
    if (!current.id) return;
    try {
      const response: {
        message: string;
        data: {
          affected_rules?: Array<{ id: number; name: string }>;
          removed_path_mappings?: number;
        };
      } = await delete_api(`/api/settings/service/${current.id}`);
      arrInstances[serviceId] = (arrInstances[serviceId] ?? []).filter(
        (x) => x.id !== current.id,
      );
      const next = (arrInstances[serviceId] ?? [])[0];
      if (next?.id) selectArrInstance(serviceId, next.id);
      else {
        serviceState[serviceId as SettingsTab] = emptyServiceState(serviceId);
        serviceState[serviceId as SettingsTab].config.enabled = false;
      }
      toast.success(response.message);
      const affectedRules = response.data.affected_rules ?? [];
      const removedMappings = response.data.removed_path_mappings ?? 0;
      if (affectedRules.length || removedMappings) {
        toast.warning(
          [
            affectedRules.length
              ? `${affectedRules.length} dependent rule(s) were disabled`
              : "",
            removedMappings
              ? `${removedMappings} scoped path mapping(s) were removed`
              : "",
          ]
            .filter(Boolean)
            .join(". "),
          { duration: 8000 },
        );
      }
    } catch (err: any) {
      toast.error(
        `Error deleting ${serviceDisplayName(serviceId)} instance: ${err.message}`,
      );
    }
  };

  // delete instance with confirm
  const deleteArrInstance = (serviceId: string) => {
    const current = serviceState[serviceId as SettingsTab].config;
    if (!current.id) return;
    openConfirmDialog(
      { kind: "delete-instance", serviceId },
      {
        title: "Delete Instance",
        description: `Permanently delete ${current.name ?? serviceDisplayName(serviceId)}? This cannot be undone.`,
        actionLabel: "Delete",
        destructive: true,
      },
    );
  };

  // handler for confirm dialog action button
  const confirmPendingAction = async () => {
    const intent = confirmIntent;
    if (!intent) return;
    closeConfirmDialog();

    if (intent.kind === "switch-instance") {
      applyArrInstanceSelection(intent.serviceId, intent.targetId);
      return;
    }

    if (intent.kind === "new-instance") {
      createNewArrInstanceDraft(intent.serviceId);
      return;
    }

    await performDeleteArrInstance(intent.serviceId);
  };

  // handler for service config changes (radarr/sonarr/seerr only)
  const handleServiceChange = (event: CustomEvent) => {
    const { field, value } = event.detail;
    serviceTestStatus[activeTab] = "idle";
    if (field === "enabled") serviceState[activeTab].config.enabled = value;
    else if (field === "name") serviceState[activeTab].config.name = value;
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

  const loadMetadataProviderStatus = async () => {
    try {
      metadataProviderStatusLoading = true;
      metadataProviderStatusError = null;
      metadataProviderStatus = await get_api<MetadataProviderStatusResponse>(
        "/api/settings/metadata-providers/status",
      );
    } catch (err: any) {
      metadataProviderStatusError =
        err?.message ?? "Failed to load metadata provider status";
    } finally {
      metadataProviderStatusLoading = false;
    }
  };

  const getMetadataProviderStatus = (serviceId: string) =>
    metadataProviderStatus?.providers.find(
      (provider) => provider.service_type === serviceId,
    ) ?? null;

  // test service connection
  const testServiceConnection = async (serviceId: string) => {
    testingService = true;
    serviceTestStatus[serviceId as SettingsTab] = "loading";
    const config = serviceState[serviceId as SettingsTab].config;
    const baseUrl = serviceBaseUrl(serviceId, config.baseUrl).replace(
      /\/+$/,
      "",
    );
    try {
      // only send api_key if the user typed a new one (backend resolves existing key otherwise)
      const payload: Record<string, unknown> = {
        service_type: serviceId,
        id: config.id,
        name: config.name || serviceDisplayName(serviceId),
        enabled: config.enabled,
        base_url: baseUrl,
      };
      if (config.apiKey) payload.api_key = config.apiKey;
      const response: boolean = await post_api(
        "/api/settings/test/service",
        payload,
      );
      if (!response) {
        throw new Error("Connection test failed");
      }
      serviceTestStatus[serviceId as SettingsTab] = "success";
      if (isMetadataProviderTab(serviceId)) {
        await loadMetadataProviderStatus();
      }
    } catch (err: any) {
      serviceTestStatus[serviceId as SettingsTab] = "error";
      toast.error(
        `Connection test for ${serviceDisplayName(serviceId)} failed: ${err.message}`,
      );
    } finally {
      testingService = false;
    }
  };

  // save service settings
  const saveServiceSettings = async (serviceId: string) => {
    savingService = true;
    const config = serviceState[serviceId as SettingsTab].config;
    const baseUrl = serviceBaseUrl(serviceId, config.baseUrl).replace(
      /\/+$/,
      "",
    );
    try {
      const response: {
        message: string;
        data: {
          service_type: string;
          id: number;
          name: string;
          enabled: boolean;
          base_url: string;
          extra_settings?: Record<string, any>;
        };
      } = await post_api("/api/settings/save/service", {
        service_type: serviceId,
        id: config.id,
        name: config.name || serviceDisplayName(serviceId),
        base_url: baseUrl,
        enabled: config.enabled,
        extra_settings: config.extraSettings,
        // only send api_key if the user typed a new one (backend resolves existing key otherwise)
        ...(config.apiKey ? { api_key: config.apiKey } : {}),
      });
      // update state: clear the typed key, keep baseUrl/enabled from response
      serviceState[serviceId as SettingsTab].config = {
        id: response.data.id,
        name: response.data.name,
        enabled: response.data.enabled,
        baseUrl: serviceBaseUrl(serviceId, response.data.base_url),
        extraSettings: resolveExtraSettings(
          response.data.extra_settings,
          serviceId,
        ),
        apiKey: "",
      };
      serviceState[serviceId as SettingsTab].apiKeyIsSet = true;
      if (
        serviceId === SettingsTab.Radarr ||
        serviceId === SettingsTab.Sonarr
      ) {
        const saved = serviceState[serviceId as SettingsTab].config;
        const existing = arrInstances[serviceId] ?? [];
        arrInstances[serviceId] = [
          ...existing.filter((item) => item.id !== saved.id),
          { ...saved },
        ].sort((a, b) => (a.name ?? "").localeCompare(b.name ?? ""));
      }
      toast.success(response.message);
      if (isMetadataProviderTab(serviceId)) {
        await loadMetadataProviderStatus();
      }
    } catch (err: any) {
      toast.error(
        `Error saving settings for ${serviceDisplayName(serviceId)}: ${err.message}`,
      );
    } finally {
      savingService = false;
    }
  };

  // load non-media-server service settings; media servers are handled by MediaServers component
  const loadServiceSettings = async () => {
    try {
      loading = true;
      const rawServices = await get_api<
        Record<
          string,
          {
            enabled: boolean;
            id?: number;
            name?: string;
            base_url: string;
            api_key: string;
            extra_settings?: Record<string, any>;
            instances?: Array<{
              id: number;
              name: string;
              enabled: boolean;
              base_url: string;
              api_key: string;
              extra_settings?: Record<string, any>;
            }>;
          }
        >
      >("/api/settings/services");

      for (const [serviceId, config] of Object.entries(rawServices)) {
        if ((MEDIA_SERVERS as readonly string[]).includes(serviceId)) continue;
        const instance =
          (serviceId === SettingsTab.Radarr ||
            serviceId === SettingsTab.Sonarr) &&
          config.instances?.length
            ? config.instances[0]
            : config;
        if (
          serviceId === SettingsTab.Radarr ||
          serviceId === SettingsTab.Sonarr
        ) {
          arrInstances[serviceId] =
            config.instances?.map((item) => ({
              id: item.id,
              name: item.name,
              enabled: item.enabled,
              baseUrl: serviceBaseUrl(serviceId, item.base_url),
              apiKey: "",
              extraSettings: resolveExtraSettings(
                item.extra_settings,
                serviceId,
              ),
            })) ?? [];
        }
        serviceState[serviceId as SettingsTab].config = {
          id: instance.id ?? null,
          name: instance.name ?? serviceDisplayName(serviceId),
          enabled: instance.enabled,
          baseUrl: serviceBaseUrl(serviceId, instance.base_url),
          apiKey: "",
          extraSettings: resolveExtraSettings(
            instance.extra_settings,
            serviceId,
          ),
        };
        serviceState[serviceId as SettingsTab].apiKeyIsSet = !!instance.api_key;
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
    if (isAdmin) {
      loadServiceSettings();
      loadMetadataProviderStatus();
    }
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
        <Spinner class="size-5 text-primary" />
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
            {#if activeTab === SettingsTab.Radarr || activeTab === SettingsTab.Sonarr}
              {#snippet instanceSelector()}
                <Label class="space-y-1">
                  <span class="text-sm font-medium text-foreground"
                    >Instance</span
                  >
                  <Select.Root
                    type="single"
                    value={serviceState[activeTab].config.id !== null
                      ? String(serviceState[activeTab].config.id)
                      : undefined}
                    onValueChange={(value) =>
                      selectArrInstance(
                        activeTab,
                        value ? Number(value) : null,
                      )}
                  >
                    <Select.Trigger size="sm" class="w-40 md:min-w-56">
                      {#if serviceState[activeTab].config.id !== null}
                        {getArrInstanceName(
                          activeTab,
                          serviceState[activeTab].config.id ?? null,
                        ) ?? "Selected instance"}
                      {:else}
                        Select an instance...
                      {/if}
                    </Select.Trigger>
                    <Select.Content>
                      {#if (arrInstances[activeTab] ?? []).length === 0}
                        <Select.Item value="__empty" disabled>
                          No instances available
                        </Select.Item>
                      {:else}
                        {#each arrInstances[activeTab] ?? [] as instance}
                          <Select.Item
                            value={String(instance.id ?? "")}
                            label={`${instance.name}${instance.enabled ? "" : " (disabled)"}`}
                          >
                            {instance.name}{instance.enabled
                              ? ""
                              : " (disabled)"}
                          </Select.Item>
                        {/each}
                      {/if}
                    </Select.Content>
                  </Select.Root>
                </Label>
              {/snippet}
              {#snippet instanceActions()}
                <div class="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    class="cursor-pointer bg-add/70 hover:bg-add/80"
                    onclick={() => newArrInstance(activeTab)}
                  >
                    <Plus class="size-3.5" />
                    New
                  </Button>
                  <Button
                    size="sm"
                    class="cursor-pointer bg-destructive/70 hover:bg-destructive/80"
                    onclick={() => deleteArrInstance(activeTab)}
                    disabled={!serviceState[activeTab].config.id}
                  >
                    <Trash2 class="size-3.5" />
                    Delete
                  </Button>
                </div>
              {/snippet}
              <InstanceManagerBar
                selector={instanceSelector}
                actions={instanceActions}
              />
            {/if}
            <ServiceConfigForm
              tabLabel={tabs.find((t) => t.id === activeTab)?.label || ""}
              tabIcon={getTabIcon(activeTab)}
              enabled={serviceState[activeTab].config.enabled}
              name={serviceState[activeTab].config.name}
              lockedName={tabs.find((t) => t.id === activeTab)?.lockName
                ? tabs.find((t) => t.id === activeTab)?.label
                : undefined}
              baseUrl={serviceState[activeTab].config.baseUrl}
              apiKey={serviceState[activeTab].config.apiKey}
              apiKeyIsSet={serviceState[activeTab].apiKeyIsSet}
              baseUrlPlaceholder={tabs.find((t) => t.id === activeTab)
                ?.baseUrlPlaceholder || "http://localhost:8096"}
              hideBaseUrl={tabs.find((t) => t.id === activeTab)?.hideBaseUrl ??
                false}
              fixedBaseUrl={DEFAULT_SERVICE_BASE_URLS[activeTab]}
              extraSettings={serviceState[activeTab].config.extraSettings ?? {}}
              onchange={handleServiceChange}
            />
            {#if isMetadataProviderTab(activeTab)}
              <div class="mt-4">
                <MetadataProviderStatusPanel
                  provider={getMetadataProviderStatus(activeTab)}
                  loading={metadataProviderStatusLoading}
                  error={metadataProviderStatusError}
                  lastCheckedAt={metadataProviderStatus?.last_checked_at}
                  lastSuccessfulRefreshAt={metadataProviderStatus?.last_successful_refresh_at}
                  lastError={metadataProviderStatus?.last_error}
                />
              </div>
            {/if}

            <!-- action buttons for service tabs -->
            <div class="flex gap-3 justify-end mt-3">
              {#if activeTab !== SettingsTab.Radarr && activeTab !== SettingsTab.Sonarr && serviceState[activeTab].config.id}
                <Button
                  onclick={() => deleteArrInstance(activeTab)}
                  disabled={savingService || testingService}
                  class="cursor-pointer gap-2 bg-destructive/80 hover:bg-destructive text-destructive-foreground"
                >
                  <Trash2 class="size-4" />
                  Delete
                </Button>
              {/if}
              <TestButton
                onclick={() => testServiceConnection(activeTab)}
                disabled={testingService || savingService}
                status={serviceTestStatus[activeTab]}
                class="cursor-pointer gap-2"
                size="default"
              >
                Test
              </TestButton>
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

            <!-- authentication settings -->
          {:else if activeTab === SettingsTab.Authentication}
            <Authentication svgIcon={getTabIcon(activeTab)} />

            <!-- tasks -->
          {:else if activeTab === SettingsTab.Tasks}
            <Tasks svgIcon={getTabIcon(activeTab)} />

            <!-- notifications -->
          {:else if activeTab === SettingsTab.Notifications}
            <Notifications
              userRole={$auth.user?.role || "user"}
              svgIcon={getTabIcon(activeTab)}
            />

            <!-- account -->
          {:else if activeTab === SettingsTab.Account}
            <Account svgIcon={getTabIcon(activeTab)} />

            <!-- users -->
          {:else if activeTab === SettingsTab.Users}
            <Users svgIcon={getTabIcon(activeTab)} />

            <!-- user signals settings -->
          {:else if activeTab === SettingsTab.UserSignals}
            <UserSignals svgIcon={getTabIcon(activeTab)} />

            <!-- about -->
          {:else if activeTab === SettingsTab.About}
            <About svgIcon={getTabIcon(activeTab)} />
          {/if}
        </div>
      </div>
    {/if}
  </div>
</div>

<AlertDialog.Root
  open={showConfirmDialog}
  onOpenChange={(value) => {
    showConfirmDialog = value;
    if (!value) confirmIntent = null;
  }}
>
  <AlertDialog.Content
    class="bg-card border border-border rounded-lg p-6 max-w-md w-full"
  >
    <AlertDialog.Header>
      <AlertDialog.Title class="text-xl font-semibold text-foreground mb-2">
        {confirmTitle}
      </AlertDialog.Title>
      <AlertDialog.Description class="text-muted-foreground">
        {confirmDescription}
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer class="flex justify-end gap-3 pt-4">
      <AlertDialog.Cancel
        class="cursor-pointer hover text-foreground bg-secondary"
        onclick={closeConfirmDialog}
      >
        Cancel
      </AlertDialog.Cancel>
      <AlertDialog.Action
        class={confirmActionClass}
        onclick={confirmPendingAction}
      >
        {confirmActionLabel}
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
