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
  import { type GeneralSettings } from "$lib/types/shared";
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
  let pathMappings = $state<{ source_prefix: string; local_prefix: string }[]>(
    [],
  );
  let moveEnabled = $state(false);
  let moveDestinationMovies = $state("");
  let moveDestinationSeries = $state("");
  let pathSuggestions = $state<string[]>([]);

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
        move_enabled: moveEnabled,
        move_destination_movies: moveDestinationMovies,
        move_destination_series: moveDestinationSeries,
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
    pathMappings = [...pathMappings, { source_prefix: "", local_prefix: "" }];
  };

  const removePathMapping = (index: number) => {
    pathMappings = pathMappings.filter((_, i) => i !== index);
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
        moveEnabled = settings.move_enabled ?? false;
        moveDestinationMovies = settings.move_destination_movies ?? "";
        moveDestinationSeries = settings.move_destination_series ?? "";
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
      <Spinner />
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
        paths are directly accessible.
      </p>

      <div class="space-y-2">
        {#if pathMappings.length > 0}
          <div class="grid grid-cols-[1fr_1fr_auto] gap-2 mb-1">
            <span class="text-xs text-muted-foreground font-medium"
              >Media server path prefix</span
            >
            <span class="text-xs text-muted-foreground font-medium"
              >Local path prefix</span
            >
            <span></span>
          </div>
        {/if}

        <!-- path mappings -->
        {#each pathMappings as mapping, i}
          <div class="grid grid-cols-[1fr_1fr_auto] gap-2 items-center">
            <Input
              type="text"
              list="path-suggestions"
              class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono text-sm"
              placeholder="/movies"
              bind:value={mapping.source_prefix}
            />
            <Input
              type="text"
              class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono text-sm"
              placeholder="/mnt/data/movies"
              bind:value={mapping.local_prefix}
            />
            <Button
              size="icon-sm"
              class="bg-destructive/70 hover:bg-destructive/80 cursor-pointer shrink-0"
              onclick={() => removePathMapping(i)}
              aria-label="Remove mapping"
            >
              <Trash2 class="size-4" />
            </Button>
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
                      { source_prefix: suggestion, local_prefix: "" },
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
