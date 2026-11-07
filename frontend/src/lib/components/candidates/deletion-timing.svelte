<script lang="ts">
  import { Input } from "$lib/components/ui/input/index.js";
  import { scheduledDeletionError } from "$lib/utils/scheduled-deletion-time.js";

  let {
    mode = $bindable<"now" | "later">("now"),
    value = $bindable(""),
    allowNow = true,
  }: { mode?: "now" | "later"; value?: string; allowNow?: boolean } = $props();
  const id = $props.id();
  const error = $derived(mode === "later" ? scheduledDeletionError(value) : "");
  const displayTime = $derived(
    mode === "later" && !error
      ? new Date(value).toLocaleString(undefined, { timeZoneName: "short" })
      : "",
  );
</script>

<div class="space-y-3 text-sm text-foreground">
  {#if allowNow}
    <fieldset class="flex flex-wrap gap-4">
      <legend class="mb-2 font-medium">When should this run?</legend>
      <label class="flex items-center gap-2"
        ><input type="radio" name={id} value="now" bind:group={mode} /> Delete now</label
      >
      <label class="flex items-center gap-2"
        ><input type="radio" name={id} value="later" bind:group={mode} /> Delete on…</label
      >
    </fieldset>
  {/if}
  {#if mode === "later"}
    <label for={`${id}-time`} class="block font-medium"
      >Deletion date and time</label
    >
    <Input
      id={`${id}-time`}
      type="datetime-local"
      bind:value
      aria-invalid={!!error}
      aria-describedby={`${id}-hint`}
    />
    <p id={`${id}-hint`} class="text-muted-foreground">
      {error ||
        `Scheduled for ${displayTime}. Runs at or after this time when Reclaimerr is available.`}
    </p>
    <p class="text-muted-foreground">
      You can reschedule or cancel before execution starts. Protection and
      active playback are checked when it runs.
    </p>
  {/if}
</div>
