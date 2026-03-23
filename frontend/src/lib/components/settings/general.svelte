<script lang="ts">
  import { onMount } from "svelte";
  import type { Component } from "svelte";
  import { Label } from "$lib/components/ui/label/index.js";
  import { Checkbox } from "$lib/components/ui/checkbox/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { get_api, put_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import { Button } from "$lib/components/ui/button/index.js";
  import Save from "@lucide/svelte/icons/save";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { type GeneralSettings } from "$lib/types/shared";

  // props
  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  // state
  let loading = $state(true);
  let savingSettings = $state(false);
  let aarrTagging = $state({
    autoTagEnabled: false,
    cleanupTagSuffix: "",
    workerPollMinSeconds: "",
    workerPollMaxSeconds: "",
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
      const validationError = validateCleanupTagSuffix();
      if (validationError) throw new Error(validationError);

      // save settings to backend
      await put_api("/api/settings/general", {
        auto_tag_enabled: aarrTagging.autoTagEnabled,
        cleanup_tag_suffix: aarrTagging.cleanupTagSuffix,
        worker_poll_min_seconds: parseOptionalSeconds(
          aarrTagging.workerPollMinSeconds,
        ),
        worker_poll_max_seconds: parseOptionalSeconds(
          aarrTagging.workerPollMaxSeconds,
        ),
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

  // check cleanup tag suffix for invalid characters and notify user if not valid
  const validateCleanupTagSuffix = (): string | void => {
    // allow empty suffix
    if (aarrTagging.cleanupTagSuffix) {
      // must start with hyphen or underscore, and only contain allowed chars
      const valid = /^[-_][a-z_-]*$/.test(aarrTagging.cleanupTagSuffix);
      if (!valid) {
        return (
          "Cleanup tag suffix must start with a hyphen or underscore and only contain lowercase letters, " +
          "underscores, or hyphens"
        );
      }
    }

    const workerPollMinSeconds = parseOptionalSeconds(
      aarrTagging.workerPollMinSeconds,
    );
    const workerPollMaxSeconds = parseOptionalSeconds(
      aarrTagging.workerPollMaxSeconds,
    );

    if (
      aarrTagging.workerPollMinSeconds.trim() &&
      workerPollMinSeconds === null
    ) {
      return "Worker poll minimum must be a valid number of seconds";
    }

    if (
      aarrTagging.workerPollMaxSeconds.trim() &&
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

  onMount(async () => {
    try {
      const settings: GeneralSettings = await get_api("/api/settings/general");
      if (settings) {
        aarrTagging = {
          autoTagEnabled: settings.auto_tag_enabled,
          cleanupTagSuffix: settings.cleanup_tag_suffix,
          workerPollMinSeconds:
            settings.worker_poll_min_seconds?.toString() ?? "",
          workerPollMaxSeconds:
            settings.worker_poll_max_seconds?.toString() ?? "",
        };
      }
    } catch (error) {
      console.error("Error fetching general settings:", error);
      toast.error("Failed to load general settings");
    } finally {
      loading = false;
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
    <!-- arr tagging -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm mt-6">
      <h3 class="font-semibold text-foreground items-center mb-3">
        Radarr and Sonarr tagging
      </h3>

      <!-- automatic tagging toggle -->
      <div class="flex gap-2 items-center mb-4">
        <Checkbox
          id="autoTagEnabled"
          name="autoTagEnabled"
          class="cursor-pointer"
          bind:checked={aarrTagging.autoTagEnabled}
        />
        <Label
          for="autoTagEnabled"
          class="inline-flex items-center gap-2 cursor-pointer"
        >
          <span class="text-sm text-foreground"
            >Enable automatic tagging of reclaimerr candidates</span
          >
        </Label>
      </div>

      <!-- cleanup tag suffix -->
      <div>
        <Label for="cleanupTagSuffix" class="mb-2">
          <span class="text-sm text-foreground">Cleanup Tag Suffix</span>
        </Label>
        <Input
          id="cleanupTagSuffix"
          name="cleanupTagSuffix"
          type="text"
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
          placeholder="e.g. '-candidate'"
          maxlength={15}
          bind:value={aarrTagging.cleanupTagSuffix}
        />
        <p class="mt-1 text-xs text-muted-foreground">
          Optional suffix for cleanup tag (base: 'reclaimerr'). Example:
          '-candidate' -> 'reclaimerr-candidate'
        </p>
        <p class="mt-1 text-xs text-muted-foreground">
          Note: modifying this will update existing tags during the <span
            class="font-bold">Tag Cleanup Candidates</span
          > task.
        </p>
      </div>
    </div>

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
            bind:value={aarrTagging.workerPollMinSeconds}
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
            bind:value={aarrTagging.workerPollMaxSeconds}
          />
        </div>
      </div>
    </div>

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
