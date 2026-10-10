<script lang="ts">
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Checkbox } from "$lib/components/ui/checkbox/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { get_api, post_api } from "$lib/api";
  import type {
    ReAddOptions,
    ReAddResponse,
    ReclaimHistoryEntry,
  } from "$lib/types/shared";
  import { toast } from "svelte-sonner";

  let {
    open = $bindable(false),
    entry,
    onSuccess,
  }: {
    open?: boolean;
    entry: ReclaimHistoryEntry | null;
    onSuccess: () => void;
  } = $props();

  let options = $state<ReAddOptions | null>(null);
  let loading = $state(false);
  let submitting = $state(false);
  let error = $state("");
  let configId = $state<number | null>(null);
  let rootFolder = $state("");
  let profileId = $state<number | null>(null);
  let search = $state(true);

  const arrLabel = $derived(
    options?.service === "sonarr" ? "Sonarr" : "Radarr",
  );
  const instance = $derived(
    options?.instances.find((i) => i.service_config_id === configId) ?? null,
  );
  // why the selected instance can't take the title, if it can't
  const blockedReason = $derived(
    !instance
      ? null
      : instance.error
        ? instance.error
        : instance.already_added
          ? `Already in ${instance.service_name}. Re-add missing files from ${arrLabel} directly.`
          : instance.root_folders.length === 0
            ? `${instance.service_name} has no root folders.`
            : instance.quality_profiles.length === 0
              ? `${instance.service_name} has no quality profiles.`
              : null,
  );

  $effect(() => {
    if (open && entry) void load(entry.id);
  });

  // default to the first folder and profile of whichever instance is picked
  $effect(() => {
    rootFolder = instance?.root_folders[0] ?? "";
    profileId = instance?.quality_profiles[0]?.id ?? null;
  });

  async function load(historyId: number) {
    options = null;
    error = "";
    search = true;
    loading = true;
    try {
      options = await get_api<ReAddOptions>(
        `/api/media/reclaim-history/${historyId}/re-add-options`,
      );
      // prefer an instance that can actually take the title
      configId =
        (
          options.instances.find((i) => !i.error && !i.already_added) ??
          options.instances[0]
        )?.service_config_id ?? null;
    } catch (e: any) {
      error = e.message ?? "Failed to load re-add options.";
    } finally {
      loading = false;
    }
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (!entry || configId === null || profileId === null || !rootFolder)
      return;
    submitting = true;
    error = "";
    try {
      const response = await post_api<ReAddResponse>(
        `/api/media/reclaim-history/${entry.id}/re-add`,
        {
          service_config_id: configId,
          root_folder_path: rootFolder,
          quality_profile_id: profileId,
          search,
        },
      );
      toast.success(response.message);
      open = false;
      onSuccess();
    } catch (e: any) {
      error = e.message ?? "Re-add failed.";
    } finally {
      submitting = false;
    }
  }
</script>

<Dialog.Root bind:open>
  <Dialog.Content class="bg-card text-foreground border-border">
    <Dialog.Header>
      <Dialog.Title
        >Re-add {options?.title ?? entry?.name ?? "title"}</Dialog.Title
      >
      <Dialog.Description>
        Adds the whole {entry?.media_type === "series" ? "series" : "movie"} back
        to {arrLabel}, monitored, and removes its import exclusion if it has
        one.
      </Dialog.Description>
    </Dialog.Header>

    {#if loading}
      <div class="flex items-center gap-2 py-6 text-sm text-muted-foreground">
        <Spinner class="size-4" />
        Looking it up in {arrLabel}…
      </div>
    {:else if options?.unavailable_reason}
      <p
        class="rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-sm"
      >
        {options.unavailable_reason}
      </p>
    {:else if options}
      <form onsubmit={submit} class="space-y-4">
        <div class="space-y-2">
          <Label for="re-add-instance">{arrLabel} instance</Label>
          <select
            id="re-add-instance"
            bind:value={configId}
            class="w-full rounded-md border border-input bg-card p-2"
          >
            {#each options.instances as option (option.service_config_id)}
              <option value={option.service_config_id}>
                {option.service_name}{option.already_added
                  ? " (already added)"
                  : option.error
                    ? " (unavailable)"
                    : ""}
              </option>
            {/each}
          </select>
        </div>

        {#if blockedReason}
          <p
            class="rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-sm"
          >
            {blockedReason}
          </p>
        {:else if instance}
          <div class="space-y-2">
            <Label for="re-add-root">Root folder</Label>
            <select
              id="re-add-root"
              bind:value={rootFolder}
              class="w-full rounded-md border border-input bg-card p-2"
            >
              {#each instance.root_folders as folder (folder)}
                <option value={folder}>{folder}</option>
              {/each}
            </select>
          </div>
          <div class="space-y-2">
            <Label for="re-add-profile">Quality profile</Label>
            <select
              id="re-add-profile"
              bind:value={profileId}
              class="w-full rounded-md border border-input bg-card p-2"
            >
              {#each instance.quality_profiles as profile (profile.id)}
                <option value={profile.id}>{profile.name}</option>
              {/each}
            </select>
          </div>
          <Label class="flex items-center gap-2 text-sm cursor-pointer">
            <Checkbox
              checked={search}
              onCheckedChange={(checked) => (search = checked === true)}
            />
            Search after adding
          </Label>
        {/if}

        {#if error}<p role="alert" class="text-sm text-destructive">
            {error}
          </p>{/if}

        <Dialog.Footer>
          <Button type="button" variant="outline" onclick={() => (open = false)}
            >Cancel</Button
          >
          <Button
            type="submit"
            disabled={submitting ||
              !!blockedReason ||
              !rootFolder ||
              profileId === null}
            >{submitting ? "Adding…" : `Add to ${arrLabel}`}</Button
          >
        </Dialog.Footer>
      </form>
    {/if}

    {#if !options && error}<p role="alert" class="text-sm text-destructive">
        {error}
      </p>{/if}
  </Dialog.Content>
</Dialog.Root>
