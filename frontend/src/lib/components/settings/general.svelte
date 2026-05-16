<script lang="ts">
  import { onMount } from "svelte";
  import type { Component } from "svelte";
  import { Label } from "$lib/components/ui/label/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { get_api, post_api, put_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import { Button } from "$lib/components/ui/button/index.js";
  import Save from "@lucide/svelte/icons/save";
  import Plus from "@lucide/svelte/icons/plus";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import PowerOff from "@lucide/svelte/icons/power-off";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import {
    MediaType,
    type GeneralSettings,
    type PathMapping,
    type PostActionWebhookConfig,
  } from "$lib/types/shared";
  import TestButton from "$lib/components/test-button.svelte";
  import { auth } from "$lib/stores/auth";

  // props
  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  // state
  let loading = $state(true);
  let savingSettings = $state(false);
  let shuttingDown = $state(false);
  let shutdownDone = $state(false);
  const isAdmin = $derived($auth.user?.role === "admin");
  let generalSettings = $state({
    workerPollMinSeconds: "",
    workerPollMaxSeconds: "",
  });
  type PathMappingScope = {
    id: number;
    service_type: string;
    name: string;
    enabled: boolean;
  };

  let pathMappings = $state<PathMapping[]>([]);
  let postActionWebhooks = $state<PostActionWebhookConfig[]>([]);
  let moveEnabled = $state(false);
  let moveDestinationMovies = $state("");
  let moveDestinationSeries = $state("");
  let mediaServerFallbackEnabled = $state(true);
  let defaultArrDeleteBehavior = $state<"unmonitor" | "remove_if_empty">(
    "unmonitor",
  );
  let pathSuggestions = $state<string[]>([]);
  let pathMappingScopes = $state<PathMappingScope[]>([]);
  let testingWebhookIndex = $state<number | null>(null);

  // default settings for webhook
  const serviceTypeOptions = ["plex", "jellyfin", "emby", "radarr", "sonarr"];
  const defaultWebhookBodyTemplate = '{"path":"{path}","action":"{action}"}';
  const emptyWebhook = (): PostActionWebhookConfig => ({
    enabled: true,
    name: "Post action webhook",
    method: "GET",
    url_template: "",
    headers: [],
    auth_username: null,
    auth_password: null,
    actions: ["deleted", "moved"],
    media_types: [MediaType.Movie, MediaType.Series],
    path_mode: "original",
    body_template: null,
    timeout_seconds: 15,
  });

  const parseOptionalSeconds = (value: string): number | null => {
    if (!value.trim()) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };

  // save settings
  const saveSettings = async () => {
    savingSettings = true;
    try {
      // validate input before saving
      const validationError = validateWorkerPoll();
      if (validationError) throw new Error(validationError);

      // save settings to backend
      await put_api("/api/settings/general", {
        worker_poll_min_seconds: parseOptionalSeconds(
          generalSettings.workerPollMinSeconds,
        ),
        worker_poll_max_seconds: parseOptionalSeconds(
          generalSettings.workerPollMaxSeconds,
        ),
        path_mappings: pathMappings.filter(
          (m) => m.source_prefix.trim() && m.local_prefix.trim(),
        ),
        post_action_webhooks: postActionWebhooks
          .filter((w) => w.name.trim() && w.url_template.trim())
          .map((w) => ({
            ...w,
            name: w.name.trim(),
            url_template: w.url_template.trim(),
            auth_username: w.auth_username?.trim() || null,
            auth_password: w.auth_password?.trim() || null,
            body_template: w.body_template?.trim() || null,
            headers: w.headers.filter((h) => h.name.trim()),
            timeout_seconds: Number(w.timeout_seconds) || 15,
          })),
        move_enabled: moveEnabled,
        move_destination_movies: moveDestinationMovies,
        move_destination_series: moveDestinationSeries,
        media_server_fallback_enabled: mediaServerFallbackEnabled,
        default_arr_delete_behavior: defaultArrDeleteBehavior,
      });
      toast.success("General settings saved");
    } catch (error) {
      console.error("Error saving general settings:", error);
      toast.error(
        `Failed to save general settings: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      savingSettings = false;
    }
  };

  const validateWorkerPoll = (): string | void => {
    const workerPollMinSeconds = parseOptionalSeconds(
      generalSettings.workerPollMinSeconds,
    );
    const workerPollMaxSeconds = parseOptionalSeconds(
      generalSettings.workerPollMaxSeconds,
    );

    if (
      generalSettings.workerPollMinSeconds.trim() &&
      workerPollMinSeconds === null
    ) {
      return "Worker poll minimum must be a valid number of seconds";
    }

    if (
      generalSettings.workerPollMaxSeconds.trim() &&
      workerPollMaxSeconds === null
    ) {
      return "Worker poll maximum must be a valid number of seconds";
    }

    if (workerPollMinSeconds !== null && workerPollMinSeconds <= 0) {
      return "Worker poll minimum must be greater than 0 seconds";
    }

    if (workerPollMinSeconds !== null && workerPollMinSeconds > 60) {
      return "Worker poll minimum cannot exceed 60 seconds";
    }

    if (workerPollMaxSeconds !== null && workerPollMaxSeconds <= 0) {
      return "Worker poll maximum must be greater than 0 seconds";
    }

    if (workerPollMaxSeconds !== null && workerPollMaxSeconds > 60) {
      return "Worker poll maximum cannot exceed 60 seconds";
    }

    if (
      workerPollMinSeconds !== null &&
      workerPollMaxSeconds !== null &&
      workerPollMinSeconds > workerPollMaxSeconds
    ) {
      return "Worker poll minimum cannot be greater than worker poll maximum";
    }
  };

  const addPathMapping = () => {
    pathMappings = [
      ...pathMappings,
      {
        source_prefix: "",
        local_prefix: "",
        service_type: null,
        service_config_id: null,
      },
    ];
  };

  const removePathMapping = (index: number) => {
    pathMappings = pathMappings.filter((_, i) => i !== index);
  };

  const mappingScopeValue = (mapping: PathMapping) => {
    if (mapping.service_config_id != null) {
      return `config:${mapping.service_config_id}`;
    }
    if (mapping.service_type) return `type:${mapping.service_type}`;
    return "global";
  };

  const setMappingScope = (mapping: PathMapping, value: string) => {
    if (value === "global") {
      mapping.service_type = null;
      mapping.service_config_id = null;
      return;
    }
    if (value.startsWith("type:")) {
      mapping.service_type = value.slice("type:".length);
      mapping.service_config_id = null;
      return;
    }
    if (value.startsWith("config:")) {
      const id = Number(value.slice("config:".length));
      const scope = pathMappingScopes.find((s) => s.id === id);
      mapping.service_type = scope?.service_type ?? null;
      mapping.service_config_id = Number.isFinite(id) ? id : null;
    }
  };

  const addWebhook = () => {
    postActionWebhooks = [...postActionWebhooks, emptyWebhook()];
  };

  const removeWebhook = (index: number) => {
    postActionWebhooks = postActionWebhooks.filter((_, i) => i !== index);
  };

  const addWebhookHeader = (webhook: PostActionWebhookConfig) => {
    webhook.headers = [...webhook.headers, { name: "", value: "" }];
  };

  const removeWebhookHeader = (
    webhook: PostActionWebhookConfig,
    index: number,
  ) => {
    webhook.headers = webhook.headers.filter((_, i) => i !== index);
  };

  const toggleWebhookValue = <T extends string>(values: T[], value: T): T[] => {
    return values.includes(value)
      ? values.filter((item) => item !== value)
      : [...values, value];
  };

  const applyAutopulsePreset = (webhook: PostActionWebhookConfig) => {
    webhook.name = webhook.name?.trim() || "Autopulse";
    webhook.method = "GET";
    webhook.url_template =
      "http://autopulse:2875/triggers/manual?path={urlencoded_path}";
    webhook.path_mode = "original";
    webhook.actions = ["deleted", "moved"];
    webhook.media_types = [MediaType.Movie, MediaType.Series];
    webhook.headers = [];
    webhook.body_template = null;
  };

  const testWebhook = async (
    webhook: PostActionWebhookConfig,
    index: number,
  ) => {
    testingWebhookIndex = index;
    try {
      const result = await post_api<{
        success: boolean;
        status_code: number | null;
        error: string | null;
      }>("/api/settings/general/webhooks/test", { webhook });
      if (result.success) {
        toast.success(`Webhook test succeeded (${result.status_code})`);
      } else {
        toast.error(`Webhook test failed: ${result.error ?? "unknown error"}`);
      }
    } catch (error) {
      toast.error(
        `Webhook test failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      testingWebhookIndex = null;
    }
  };

  // shutdown app (desktop mode ONLY and requires admin)
  const shutdownApp = async () => {
    if (shuttingDown || shutdownDone) return;
    shuttingDown = true;
    try {
      await post_api("/api/system/shutdown", {});
      shutdownDone = true;
      toast.success("Shutting down… you can close this tab.");
    } catch (error: unknown) {
      const status =
        error && typeof error === "object" && "status" in error
          ? (error as { status: number }).status
          : null;
      if (status === 503) {
        toast.info("Shutdown is not available in server mode.");
      } else {
        toast.error("Failed to shut down the application.");
      }
    } finally {
      shuttingDown = false;
    }
  };

  onMount(async () => {
    try {
      const settings: GeneralSettings = await get_api("/api/settings/general");
      if (settings) {
        generalSettings = {
          workerPollMinSeconds:
            settings.worker_poll_min_seconds?.toString() ?? "",
          workerPollMaxSeconds:
            settings.worker_poll_max_seconds?.toString() ?? "",
        };
        pathMappings = settings.path_mappings ?? [];
        postActionWebhooks = settings.post_action_webhooks ?? [];
        moveEnabled = settings.move_enabled ?? false;
        moveDestinationMovies = settings.move_destination_movies ?? "";
        moveDestinationSeries = settings.move_destination_series ?? "";
        mediaServerFallbackEnabled =
          settings.media_server_fallback_enabled ?? true;
        defaultArrDeleteBehavior =
          settings.default_arr_delete_behavior ?? "unmonitor";
      }
    } catch (error) {
      console.error("Error fetching general settings:", error);
      toast.error("Failed to load general settings");
    } finally {
      loading = false;
    }

    // fetch path suggestions separately (not really critical and we'll ignore errors)
    try {
      const suggestions = await get_api<string[]>(
        "/api/settings/general/path-suggestions",
      );
      pathSuggestions = suggestions ?? [];
    } catch {
      // ignore all errors
    }

    try {
      const scopes = await get_api<PathMappingScope[]>(
        "/api/settings/general/path-mapping-scopes",
      );
      pathMappingScopes = scopes ?? [];
    } catch {
      // ignore all errors
    }
  });
</script>

<div class="space-y-6">
  <!-- header -->
  <div>
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {#if svgIcon}
        {@const Icon = svgIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      General
    </h2>
    <p class="text-sm text-muted-foreground mt-1">Manage general settings</p>
  </div>

  <!-- check if loading -->
  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner class="text-primary" />
    </div>
  {:else}
    <!-- worker polling -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm mt-6">
      <h3 class="font-semibold text-foreground items-center">Worker polling</h3>
      <p class="text-muted-foreground text-sm mb-3">
        Configure the background worker's polling behavior when idle. Adjusting
        these settings can help balance prompt job processing with resource
        usage. Recommended values are between 0.5 and 5 seconds (max 60
        seconds).
      </p>

      <div class="grid gap-4 md:grid-cols-2">
        <!-- minimum poll seconds -->
        <div>
          <Label for="workerPollMinSeconds" class="mb-2">
            <span class="text-sm text-foreground">Minimum Poll Seconds</span>
          </Label>
          <Input
            id="workerPollMinSeconds"
            name="workerPollMinSeconds"
            type="number"
            min="0.1"
            max="60"
            step="0.1"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            placeholder="Default: 0.5"
            bind:value={generalSettings.workerPollMinSeconds}
          />
        </div>

        <!-- maximum poll seconds -->
        <div>
          <Label for="workerPollMaxSeconds" class="mb-2">
            <span class="text-sm text-foreground">Maximum Poll Seconds</span>
          </Label>
          <Input
            id="workerPollMaxSeconds"
            name="workerPollMaxSeconds"
            type="number"
            min="0.1"
            max="60"
            step="0.1"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            placeholder="Default: 5"
            bind:value={generalSettings.workerPollMaxSeconds}
          />
        </div>
      </div>
    </div>

    <!-- path mappings -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <h3 class="font-semibold text-foreground">Path Mappings</h3>
      <p class="text-muted-foreground text-sm mb-3">
        Map media server paths to local filesystem paths. Required when
        Reclaimerr runs on the host but your media server reports
        Docker/container paths (e.g. <code class="font-mono text-xs"
          >/movies</code
        >
        →
        <code class="font-mono text-xs">/mnt/data/movies</code>). Leave empty if
        paths are directly accessible. Scope mappings when multiple services
        report similar container paths differently.
      </p>

      <div class="space-y-2">
        <!-- path mappings -->
        {#each pathMappings as mapping, i}
          <div
            class="grid gap-2 items-center md:grid-cols-[1fr_1fr_12rem_auto]"
          >
            <!-- media server prefix -->
            <div>
              <Label class="mb-2 text-xs">Media server path prefix</Label>
              <Input
                type="text"
                list="path-suggestions"
                class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono text-sm"
                placeholder="/movies"
                bind:value={mapping.source_prefix}
              />
            </div>

            <!-- local prefix -->
            <div>
              <Label class="mb-2 text-xs">Local path prefix</Label>
              <Input
                type="text"
                class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono text-sm"
                placeholder="/mnt/data/movies"
                bind:value={mapping.local_prefix}
              />
            </div>

            <!-- scope -->
            <div>
              <Label class="mb-2 text-xs">Scope</Label>
              <div class="inline-flex items-center gap-2 w-full">
                <Select.Root
                  type="single"
                  value={mappingScopeValue(mapping)}
                  onValueChange={(value) => setMappingScope(mapping, value)}
                >
                  <Select.Trigger class="w-full cursor-pointer text-foreground">
                    {#if mappingScopeValue(mapping) === "global"}
                      Global
                    {:else if mappingScopeValue(mapping).startsWith("type:")}
                      Any {mappingScopeValue(mapping).slice("type:".length)}
                    {:else if pathMappingScopes.find((s) => `config:${s.id}` === mappingScopeValue(mapping))}
                      {@const scope = pathMappingScopes.find(
                        (s) => `config:${s.id}` === mappingScopeValue(mapping),
                      )}
                      {scope?.name} ({scope?.service_type})
                    {/if}
                  </Select.Trigger>
                  <Select.Content>
                    <Select.Item value="global" class="cursor-pointer"
                      >Global</Select.Item
                    >
                    {#if serviceTypeOptions.length > 0}
                      <Select.Separator />
                      {#each serviceTypeOptions as serviceType}
                        <Select.Item
                          value={`type:${serviceType}`}
                          class="cursor-pointer">Any {serviceType}</Select.Item
                        >
                      {/each}
                    {/if}
                    {#if pathMappingScopes.length > 0}
                      <Select.Separator />
                      <Select.Label>Configured services</Select.Label>
                      {#each pathMappingScopes as scope}
                        <Select.Item
                          value={`config:${scope.id}`}
                          class="cursor-pointer"
                          >{scope.name} ({scope.service_type})</Select.Item
                        >
                      {/each}
                    {/if}
                  </Select.Content>
                </Select.Root>

                <!-- remove mapping button -->
                <Button
                  size="icon-sm"
                  class="bg-destructive/70 hover:bg-destructive/80 cursor-pointer shrink-0"
                  onclick={() => removePathMapping(i)}
                  aria-label="Remove mapping"
                >
                  <Trash2 class="size-4" />
                </Button>
              </div>
            </div>
          </div>
        {/each}

        <!-- path suggestions -->
        {#if pathSuggestions.length > 0}
          <datalist id="path-suggestions">
            {#each pathSuggestions as suggestion}
              <option value={suggestion}></option>
            {/each}
          </datalist>
          <div class="flex flex-wrap items-center gap-1.5 pt-1">
            <span class="text-xs text-muted-foreground shrink-0">Detected:</span
            >
            <!-- suggestion button chips -->
            {#each pathSuggestions as suggestion}
              <button
                type="button"
                class="cursor-pointer rounded border border-border bg-muted/40 px-1.5 py-0.5
                  font-mono text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
                onclick={() => {
                  const empty = pathMappings.findIndex(
                    (m) => !m.source_prefix.trim(),
                  );
                  if (empty !== -1) {
                    pathMappings[empty].source_prefix = suggestion;
                  } else {
                    pathMappings = [
                      ...pathMappings,
                      {
                        source_prefix: suggestion,
                        local_prefix: "",
                        service_type: null,
                        service_config_id: null,
                      },
                    ];
                  }
                }}>{suggestion}</button
              >
            {/each}
          </div>
        {/if}
      </div>

      <!-- add mapping button -->
      <Button
        size="sm"
        class="mt-3 cursor-pointer gap-2"
        onclick={addPathMapping}
      >
        <Plus class="size-4" />
        Add mapping
      </Button>
    </div>

    <!-- post action webhooks -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <div class="flex items-center justify-between gap-3 mb-1">
        <div>
          <h3 class="font-semibold text-foreground">Post Action Webhooks</h3>
          <p class="text-muted-foreground text-sm">
            Notify Autopulse or a custom endpoint after Reclaimerr successfully
            deletes or moves media. Failures are logged but do not roll back the
            reclaim action.
          </p>
        </div>
        <Button size="sm" class="cursor-pointer gap-2" onclick={addWebhook}>
          <Plus class="size-4" />
          Add webhook
        </Button>
      </div>

      {#if postActionWebhooks.length === 0}
        <p class="text-xs text-muted-foreground mt-3">
          No webhooks configured. Add one or use the Autopulse preset to call
          <code class="font-mono">/triggers/manual</code> with the affected path.
        </p>
      {/if}

      <div class="space-y-4 mt-4">
        {#each postActionWebhooks as webhook, i}
          <div class="rounded-lg border bg-background/40 p-3 space-y-3">
            <div class="flex items-center flex-wrap justify-between gap-3">
              <!-- toggle -->
              <div class="flex items-center gap-2">
                <Switch bind:checked={webhook.enabled} />
                <span class="text-sm font-medium text-foreground">
                  {webhook.name || `Webhook ${i + 1}`}
                </span>
              </div>
              <div class="flex items-center gap-2">
                <!-- autopulse preset button -->
                <Button
                  size="sm"
                  type="button"
                  class="cursor-pointer"
                  onclick={() => applyAutopulsePreset(webhook)}
                >
                  Autopulse preset
                </Button>

                <!-- test button -->
                <TestButton
                  onclick={() => testWebhook(webhook, i)}
                  disabled={testingWebhookIndex === i}
                  loading={testingWebhookIndex === i}
                  size="sm">Test</TestButton
                >

                <!-- delete button -->
                <Button
                  size="icon-sm"
                  class="bg-destructive/70 hover:bg-destructive/80 cursor-pointer"
                  onclick={() => removeWebhook(i)}
                  aria-label="Remove webhook"
                >
                  <Trash2 class="size-4" />
                </Button>
              </div>
            </div>

            <div class="grid gap-3 md:grid-cols-[1fr_7rem_9rem]">
              <!-- name input -->
              <div>
                <Label class="mb-2">
                  <span class="text-sm text-foreground">Name</span>
                </Label>
                <Input
                  class="input-hover-el text-foreground placeholder:text-muted-foreground"
                  placeholder="Autopulse"
                  bind:value={webhook.name}
                />
              </div>

              <!-- method input -->
              <div>
                <Label class="mb-2">
                  <span class="text-sm text-foreground">Method</span>
                </Label>
                <Select.Root type="single" bind:value={webhook.method}>
                  <Select.Trigger class="w-full cursor-pointer text-foreground">
                    {webhook.method}
                  </Select.Trigger>
                  <Select.Content>
                    <Select.Item value="GET" class="cursor-pointer"
                      >GET</Select.Item
                    >
                    <Select.Item value="POST" class="cursor-pointer"
                      >POST</Select.Item
                    >
                  </Select.Content>
                </Select.Root>
              </div>

              <!-- path mode input -->
              <div>
                <Label class="mb-2">
                  <span class="text-sm text-foreground">Path sent as</span>
                </Label>
                <Select.Root type="single" bind:value={webhook.path_mode}>
                  <Select.Trigger class="w-full cursor-pointer text-foreground">
                    {webhook.path_mode === "original"
                      ? "Original"
                      : webhook.path_mode === "local"
                        ? "Local mapped"
                        : "Destination"}
                  </Select.Trigger>
                  <Select.Content>
                    <Select.Item value="original" class="cursor-pointer"
                      >Original</Select.Item
                    >
                    <Select.Item value="local" class="cursor-pointer"
                      >Local mapped</Select.Item
                    >
                    <Select.Item value="destination" class="cursor-pointer"
                      >Destination</Select.Item
                    >
                  </Select.Content>
                </Select.Root>
              </div>
            </div>

            <!-- URL template input -->
            <div>
              <Label class="mb-2">
                <span class="text-sm text-foreground">URL template</span>
              </Label>
              <Input
                class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono text-sm"
                placeholder="http://autopulse:2875/triggers/manual?path={'{'}urlencoded_path{'}'}"
                bind:value={webhook.url_template}
              />
              <p class="text-xs text-muted-foreground mt-1">
                Template variables include
                <code class="font-mono">{`{path}`}</code>,
                <code class="font-mono">{`{urlencoded_path}`}</code>,
                <code class="font-mono">{`{title}`}</code>,
                <code class="font-mono">{`{action}`}</code>, and
                <code class="font-mono">{`{media_type}`}</code>.
              </p>
            </div>

            <!-- actions input -->
            <div class="grid gap-3 md:grid-cols-3">
              <div class="text-foreground">
                <Label class="mb-2">
                  <span class="text-sm">Actions</span>
                </Label>
                <div class="flex flex-wrap gap-3 text-sm">
                  <Label class="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      class="cursor-pointer"
                      checked={webhook.actions.includes("deleted")}
                      onchange={() =>
                        (webhook.actions = toggleWebhookValue(
                          webhook.actions,
                          "deleted",
                        ))}
                    />
                    Deleted
                  </Label>
                  <Label class="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      class="cursor-pointer"
                      checked={webhook.actions.includes("moved")}
                      onchange={() =>
                        (webhook.actions = toggleWebhookValue(
                          webhook.actions,
                          "moved",
                        ))}
                    />
                    Moved
                  </Label>
                </div>
              </div>

              <!-- media types input -->
              <div class="text-foreground">
                <Label class="mb-2">
                  <span class="text-sm">Media types</span>
                </Label>
                <div class="flex flex-wrap gap-3 text-sm">
                  <Label class="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      class="cursor-pointer"
                      checked={webhook.media_types.includes(MediaType.Movie)}
                      onchange={() =>
                        (webhook.media_types = toggleWebhookValue(
                          webhook.media_types,
                          MediaType.Movie,
                        ))}
                    />
                    Movies
                  </Label>
                  <Label class="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      class="cursor-pointer"
                      checked={webhook.media_types.includes(MediaType.Series)}
                      onchange={() =>
                        (webhook.media_types = toggleWebhookValue(
                          webhook.media_types,
                          MediaType.Series,
                        ))}
                    />
                    Series
                  </Label>
                </div>
              </div>

              <!-- timeout input -->
              <div>
                <Label class="mb-2">
                  <span class="text-sm text-foreground">Timeout seconds</span>
                </Label>
                <Input
                  type="number"
                  min="1"
                  max="120"
                  class="input-hover-el text-foreground placeholder:text-muted-foreground"
                  bind:value={webhook.timeout_seconds}
                />
              </div>
            </div>

            <!-- basic auth inputs -->
            <div class="grid gap-3 md:grid-cols-2">
              <div>
                <Label class="mb-2">
                  <span class="text-sm text-foreground"
                    >Basic auth username</span
                  >
                </Label>
                <Input
                  class="input-hover-el text-foreground placeholder:text-muted-foreground"
                  placeholder="Optional"
                  bind:value={webhook.auth_username}
                />
              </div>
              <div>
                <Label class="mb-2">
                  <span class="text-sm text-foreground"
                    >Basic auth password</span
                  >
                </Label>
                <Input
                  type="password"
                  class="input-hover-el text-foreground placeholder:text-muted-foreground"
                  placeholder="Optional"
                  bind:value={webhook.auth_password}
                />
              </div>
            </div>

            <!-- body template input -->
            {#if webhook.method === "POST"}
              <div>
                <Label class="mb-2">
                  <span class="text-sm text-foreground">Body template</span>
                </Label>
                <textarea
                  class="input-hover-el min-h-24 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm text-foreground placeholder:text-muted-foreground"
                  placeholder={defaultWebhookBodyTemplate}
                  bind:value={webhook.body_template}
                ></textarea>
                <p class="text-xs text-muted-foreground mt-1">
                  Leave empty to send the full event as JSON.
                </p>
              </div>
            {/if}

            <!-- headers input -->
            <div class="space-y-2">
              <div class="flex items-center justify-between">
                <Label>
                  <span class="text-sm text-foreground">Headers</span>
                </Label>
                <Button
                  size="sm"
                  type="button"
                  class="cursor-pointer"
                  onclick={() => addWebhookHeader(webhook)}
                >
                  Add header
                </Button>
              </div>
              {#each webhook.headers as header, headerIndex}
                <div class="grid gap-2 md:grid-cols-[1fr_1fr_auto]">
                  <Input
                    class="input-hover-el text-foreground placeholder:text-muted-foreground"
                    placeholder="Header name"
                    bind:value={header.name}
                  />
                  <Input
                    class="input-hover-el text-foreground placeholder:text-muted-foreground"
                    placeholder="Header value"
                    bind:value={header.value}
                  />
                  <Button
                    size="icon-sm"
                    class="bg-destructive/70 hover:bg-destructive/80 cursor-pointer"
                    onclick={() => removeWebhookHeader(webhook, headerIndex)}
                    aria-label="Remove header"
                  >
                    <Trash2 class="size-4" />
                  </Button>
                </div>
              {/each}
            </div>
          </div>
        {/each}
      </div>
    </div>

    <!-- move settings -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <div class="flex items-center justify-between mb-1">
        <h3 class="font-semibold text-foreground">Move Instead of Delete</h3>
        <Switch id="moveEnabled" bind:checked={moveEnabled} />
      </div>
      <p class="text-muted-foreground text-sm mb-3">
        When enabled, reclaim actions will move media files to a destination
        folder instead of deleting them, allowing manual review before permanent
        removal.
      </p>

      {#if moveEnabled}
        <div class="grid gap-4 md:grid-cols-2">
          <div>
            <Label for="moveDestinationMovies" class="mb-2">
              <span class="text-sm text-foreground">Movies Destination</span>
            </Label>
            <Input
              id="moveDestinationMovies"
              name="moveDestinationMovies"
              type="text"
              class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono"
              placeholder="/mnt/data/reclaimed/movies"
              bind:value={moveDestinationMovies}
            />
          </div>
          <div>
            <Label for="moveDestinationSeries" class="mb-2">
              <span class="text-sm text-foreground">Series Destination</span>
            </Label>
            <Input
              id="moveDestinationSeries"
              name="moveDestinationSeries"
              type="text"
              class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono"
              placeholder="/mnt/data/reclaimed/series"
              bind:value={moveDestinationSeries}
            />
          </div>
        </div>
        <p class="text-xs text-muted-foreground mt-2">
          Local paths where reclaimed files will be placed.
        </p>
      {/if}
    </div>

    <!-- media server fallback deletion -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <div class="flex items-center justify-between mb-1">
        <h3 class="font-semibold text-foreground">
          Allow Media Server Fallback Deletion
        </h3>
        <Switch
          id="mediaServerFallbackEnabled"
          bind:checked={mediaServerFallbackEnabled}
        />
      </div>
      <p class="text-muted-foreground text-sm">
        When Radarr/Sonarr cannot handle a deletion (e.g. a partial version
        deletion, or the item is not tracked in any arr instance), fall back to
        deleting via the media server (Jellyfin/Emby/Plex) directly. Disable
        this if your media server has read only file access.
      </p>
    </div>

    <!-- default ARR delete behavior -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <h3 class="font-semibold text-foreground mb-1">
        Default ARR Delete Behavior
      </h3>
      <p class="text-muted-foreground text-sm mb-3">
        Fallback behavior for deletes that are not tied to a matched cleanup
        rule, such as approved delete requests. Rule-level ARR actions still
        override this.
      </p>
      <div class="max-w-md">
        <Label for="defaultArrDeleteBehavior" class="mb-2">
          <span class="text-sm text-foreground">Fallback ARR Action</span>
        </Label>
        <Select.Root
          type="single"
          bind:value={defaultArrDeleteBehavior}
          name="defaultArrDeleteBehavior"
        >
          <Select.Trigger
            id="defaultArrDeleteBehavior"
            class="w-full cursor-pointer text-foreground"
          >
            {defaultArrDeleteBehavior === "remove_if_empty"
              ? "Remove from ARR when empty"
              : "Unmonitor after deletion"}
          </Select.Trigger>
          <Select.Content>
            <Select.Item value="unmonitor" class="cursor-pointer">
              Unmonitor after deletion
            </Select.Item>
            <Select.Item value="remove_if_empty" class="cursor-pointer">
              Remove from ARR when empty
            </Select.Item>
          </Select.Content>
        </Select.Root>
      </div>
      <p class="text-xs text-muted-foreground mt-2">
        <code>Unmonitor</code> keeps the ARR entry but prevents re-grabs.
        <code>Remove when empty</code> removes the ARR item only after the last remaining
        file for that movie or series is gone.
      </p>
    </div>

    <!-- shutdown (desktop mode / admin only) -->
    {#if isAdmin}
      <div
        class="bg-muted/50 border border-destructive/30 rounded-lg p-4 shadow-sm"
      >
        <h3 class="font-semibold text-foreground">Shutdown</h3>
        <p class="text-muted-foreground text-sm mb-3">
          Stop the Reclaimerr desktop process. Only available when running in
          desktop mode. Use the tray icon or this button to exit cleanly.
        </p>
        <Button
          variant="destructive"
          size="sm"
          class="cursor-pointer gap-2"
          disabled={shuttingDown || shutdownDone}
          onclick={shutdownApp}
        >
          {#if shuttingDown}
            <Spinner class="size-4" />
          {:else}
            <PowerOff class="size-4" />
          {/if}
          {shutdownDone ? "Shutting down…" : "Shutdown"}
        </Button>
      </div>
    {/if}

    <!-- save -->
    <div class="flex gap-3 justify-end">
      <Button
        onclick={saveSettings}
        disabled={savingSettings}
        class="cursor-pointer gap-2"
      >
        {#if savingSettings}
          <Spinner class="size-4" />
        {:else}
          <Save class="size-4" />
        {/if}
        Save
      </Button>
    </div>
  {/if}
</div>
