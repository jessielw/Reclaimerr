<script lang="ts">
  import { put_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { toast } from "svelte-sonner";
  import { truncateString } from "$lib/utils/strings";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import { ScheduleType } from "$lib/types/shared";
  import { formatIntervalDisplay } from "$lib/utils/strings";

  interface Props {
    open: boolean;
    taskId: string;
    taskName: string;
    scheduleType: ScheduleType;
    scheduleValue: string;
    defaultScheduleType: ScheduleType;
    defaultScheduleValue: string;
    enabled: boolean;
    onClose: () => void;
    onSuccess: () => void;
  }

  // preset intervals (in seconds)
  const intervalPresets = [
    { label: "Every 15 minutes", value: "900" },
    { label: "Every 30 minutes", value: "1800" },
    { label: "Every hour", value: "3600" },
    { label: "Every 2 hours", value: "7200" },
    { label: "Every 4 hours", value: "14400" },
    { label: "Every 6 hours", value: "21600" },
    { label: "Every 8 hours", value: "28800" },
    { label: "Every 12 hours", value: "43200" },
    { label: "Every 24 hours", value: "86400" },
    { label: "Custom", value: "custom" },
  ];

  // cron presets
  const cronPresets = [
    { label: "Every hour", value: "0 * * * *" },
    { label: "Every day at midnight", value: "0 0 * * *" },
    { label: "Every day at 2 AM", value: "0 2 * * *" },
    { label: "Every day at 6 AM", value: "0 6 * * *" },
    { label: "Every day at noon", value: "0 12 * * *" },
    { label: "Every Sunday at 2 AM", value: "0 2 * * 0" },
    { label: "Every Monday at 3 AM", value: "0 3 * * 1" },
    { label: "First of month at midnight", value: "0 0 1 * *" },
    { label: "Custom", value: "custom" },
  ];

  let {
    open = $bindable(),
    taskId,
    taskName,
    scheduleType: initialScheduleType,
    scheduleValue: initialScheduleValue,
    defaultScheduleType,
    defaultScheduleValue,
    enabled,
    onClose,
    onSuccess,
  }: Props = $props();

  let scheduleType = $state<ScheduleType>(ScheduleType.Interval);
  let scheduleValue = $state("");
  let saving = $state(false);

  let selectedPreset = $state<string>("custom");
  let customValue = $state("");

  // derive display values for select triggers
  const scheduleTypeDisplay = $derived(
    scheduleType === ScheduleType.Interval
      ? "Interval (every X seconds/hours)"
      : scheduleType === ScheduleType.Cron
        ? "Cron Expression"
        : "Select schedule type",
  );

  // derive display for interval preset
  const intervalPresetDisplay = $derived(() => {
    const preset = intervalPresets.find((p) => p.value === selectedPreset);
    return preset?.label ?? "Select interval";
  });

  // derive display for cron preset
  const cronPresetDisplay = $derived(() => {
    const preset = cronPresets.find((p) => p.value === selectedPreset);
    return preset?.label ?? "Select schedule";
  });

  // initialize from props only once when dialog opens
  let initialized = false;
  $effect(() => {
    if (open && !initialized) {
      scheduleType = initialScheduleType;
      scheduleValue = initialScheduleValue;

      if (initialScheduleType === ScheduleType.Interval) {
        const preset = intervalPresets.find(
          (p) => p.value === initialScheduleValue,
        );
        if (preset) {
          selectedPreset = preset.value;
        } else {
          selectedPreset = "custom";
          customValue = initialScheduleValue;
        }
      } else {
        const preset = cronPresets.find(
          (p) => p.value === initialScheduleValue,
        );
        if (preset) {
          selectedPreset = preset.value;
        } else {
          selectedPreset = "custom";
          customValue = initialScheduleValue;
        }
      }
      initialized = true;
    } else if (!open) {
      initialized = false;
    }
  });

  // sync scheduleValue when preset changes
  $effect(() => {
    if (selectedPreset !== "custom") {
      scheduleValue = selectedPreset;
    } else {
      scheduleValue = customValue;
    }
  });

  // reset values when schedule type changes (after initialization)
  $effect(() => {
    if (initialized) {
      // this will run when scheduleType changes
      scheduleType;
      // reset to clean state for the new schedule type
      selectedPreset = "custom";
      scheduleValue = "";
      customValue = "";
    }
  });

  // types for jobs list (from Tasks.svelte)
  const handleCustomValueChange = (e: Event) => {
    const target = e.target as HTMLInputElement;
    customValue = target.value;
  };

  // reset to default schedule
  const resetToDefault = () => {
    if (!defaultScheduleType || !defaultScheduleValue) {
      toast.error("No default schedule available");
      return;
    }

    scheduleType = defaultScheduleType;
    scheduleValue = defaultScheduleValue;

    // update preset selection
    if (defaultScheduleType === ScheduleType.Interval) {
      const preset = intervalPresets.find(
        (p) => p.value === defaultScheduleValue,
      );
      if (preset) {
        selectedPreset = preset.value;
      } else {
        selectedPreset = "custom";
        customValue = defaultScheduleValue;
      }
    } else {
      const preset = cronPresets.find((p) => p.value === defaultScheduleValue);
      if (preset) {
        selectedPreset = preset.value;
      } else {
        selectedPreset = "custom";
        customValue = defaultScheduleValue;
      }
    }

    toast.success("Reset to default schedule");
  };

  // save changes
  const handleSave = async () => {
    if (!scheduleValue.trim()) {
      toast.error("Schedule value is required");
      return;
    }

    // validate interval is a number
    if (scheduleType === ScheduleType.Interval) {
      const num = parseInt(scheduleValue);
      if (isNaN(num) || num <= 0) {
        toast.error("Interval must be a positive number (seconds)");
        return;
      }
    }

    saving = true;
    try {
      await put_api(`/api/tasks/tasks/${taskId}/schedule`, {
        schedule_type: scheduleType,
        schedule_value: scheduleValue,
        enabled: enabled,
      });

      toast.success(`Schedule updated for ${taskName}`);
      onSuccess();
      open = false;
    } catch (error: any) {
      console.error("Failed to update schedule:", error);
      toast.error(
        `Failed to update schedule: ${error.message || "Unknown error"}`,
      );
    } finally {
      saving = false;
    }
  };
</script>

<Dialog.Root bind:open>
  <Dialog.Content class="sm:max-w-125 text-foreground">
    <Dialog.Header>
      <Dialog.Title>Edit: {truncateString(taskName, 40)}</Dialog.Title>
      <Dialog.Description>
        Configure when this task should run
      </Dialog.Description>
    </Dialog.Header>

    <div class="space-y-4 py-4">
      <!-- schedule type -->
      <div class="space-y-2">
        <Label>Schedule Type</Label>
        <Select.Root type="single" bind:value={scheduleType}>
          <Select.Trigger class="w-full">
            {scheduleTypeDisplay}
          </Select.Trigger>
          <Select.Content>
            <Select.Item
              value="interval"
              label="Interval (every X seconds/hours)"
            >
              Interval (every X seconds/hours)
            </Select.Item>
            <Select.Item value="cron" label="Cron Expression">
              Cron Expression
            </Select.Item>
          </Select.Content>
        </Select.Root>
      </div>

      <!-- interval presets -->
      {#if scheduleType === ScheduleType.Interval}
        <div class="space-y-2">
          <Label>Interval</Label>
          <Select.Root type="single" bind:value={selectedPreset}>
            <Select.Trigger class="w-full">
              {intervalPresetDisplay()}
            </Select.Trigger>
            <Select.Content>
              {#each intervalPresets as preset}
                <Select.Item value={preset.value} label={preset.label}>
                  {preset.label}
                </Select.Item>
              {/each}
            </Select.Content>
          </Select.Root>
        </div>

        {#if selectedPreset === "custom"}
          <div class="space-y-2">
            <Label for="customInterval">Custom Interval (seconds)</Label>
            <Input
              id="customInterval"
              type="number"
              min="1"
              value={customValue}
              oninput={handleCustomValueChange}
              placeholder="e.g., 3600 for 1 hour"
            />
            {#if customValue && !isNaN(parseInt(customValue))}
              <p class="text-sm text-muted-foreground">
                = {formatIntervalDisplay(customValue)}
              </p>
            {/if}
          </div>
        {/if}
      {/if}

      <!-- cron presets -->
      {#if scheduleType === ScheduleType.Cron}
        <div class="space-y-2">
          <Label>Cron Schedule</Label>
          <Select.Root type="single" bind:value={selectedPreset}>
            <Select.Trigger class="w-full">
              {cronPresetDisplay()}
            </Select.Trigger>
            <Select.Content>
              {#each cronPresets as preset}
                <Select.Item value={preset.value} label={preset.label}>
                  {preset.label}
                </Select.Item>
              {/each}
            </Select.Content>
          </Select.Root>
        </div>

        {#if selectedPreset === "custom"}
          <div class="space-y-2">
            <Label for="customCron">Custom Cron Expression</Label>
            <Input
              id="customCron"
              type="text"
              value={customValue}
              oninput={handleCustomValueChange}
              placeholder="e.g., 0 2 * * * for 2 AM daily"
            />
            <p class="text-xs text-muted-foreground">
              Format: minute hour day month weekday
            </p>
          </div>
        {/if}
      {/if}

      <!-- current schedule preview -->
      {#if scheduleValue}
        <div class="rounded-md bg-muted p-3">
          <p class="text-sm font-medium">Current Schedule:</p>
          <p class="text-sm text-muted-foreground">
            {#if scheduleType === ScheduleType.Interval}
              Every {formatIntervalDisplay(scheduleValue)}
            {:else}
              {scheduleValue}
            {/if}
          </p>
        </div>
      {/if}
    </div>

    <Dialog.Footer>
      <div class="flex flex-col sm:flex-row w-full sm:justify-between gap-2">
        <Button
          variant="secondary"
          onclick={resetToDefault}
          disabled={saving || !defaultScheduleType || !defaultScheduleValue}
        >
          Reset to Default
        </Button>
        <div class="flex gap-2">
          <Button
            variant="outline"
            onclick={() => (open = false)}
            disabled={saving}
          >
            Cancel
          </Button>
          <Button onclick={handleSave} disabled={saving}>
            {#if saving}
              <Spinner class="mr-2 h-4 w-4" />
            {/if}
            Save Changes
          </Button>
        </div>
      </div>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
