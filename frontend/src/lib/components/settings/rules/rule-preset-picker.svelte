<script lang="ts">
  import { onMount } from "svelte";
  import { get_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Checkbox } from "$lib/components/ui/checkbox/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import Notice from "$lib/components/notice.svelte";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import {
    type LibraryType,
    type RuleDraft,
    type RulePreset,
  } from "$lib/types/shared";
  import { createPresetDraft } from "./rule-presets.js";

  let {
    libraries,
    onSelect,
    onCancel,
  }: {
    libraries: LibraryType[];
    onSelect: (draft: RuleDraft, title: string) => void;
    onCancel: () => void;
  } = $props();

  let loading = $state(true);
  let error = $state("");
  let catalog = $state<RulePreset[]>([]);
  let selected = $state<RulePreset | null>(null);
  let libraryIds = $state<string[]>([]);
  let usernames = $state<string[]>([]);
  let users = $state<{ username: string }[]>([]);
  let usersError = $state("");
  let availableServices = $state<string[]>([]);
  let servicesError = $state("");
  let targetScope =
    $state<NonNullable<RuleDraft["target_scope"]>>("movie_version");
  const missingServices = $derived(
    selected?.required_services.filter(
      (service) => !availableServices.includes(service),
    ) ?? [],
  );
  const matchingLibraries = $derived(
    libraries.filter(
      (library) =>
        library.mediaType ===
        (targetScope === "movie_version" ? "movie" : "series"),
    ),
  );
  const needsUsers = $derived(
    selected?.required_inputs.includes("playback.fully_watched_usernames") ??
      false,
  );
  const ready = $derived(
    selected !== null &&
      libraryIds.length > 0 &&
      missingServices.length === 0 &&
      (!needsUsers || (usernames.length > 0 && !usersError)),
  );

  function selectPreset(preset: RulePreset) {
    selected = preset;
    targetScope = preset.target_scopes[0];
    libraryIds = [];
    usernames = [];
    error = "";
  }

  function openDraft() {
    if (!selected || !ready) return;
    try {
      const draft = createPresetDraft(selected, {
        libraryIds,
        usernames,
        libraries,
        users,
        targetScope,
        availableServices,
      });
      onSelect(draft, selected.title);
    } catch (err) {
      error =
        err instanceof Error ? err.message : "Could not prepare this preset.";
    }
  }

  onMount(async () => {
    const results = await Promise.allSettled([
      get_api<RulePreset[]>("/api/rules/presets"),
      get_api<{ username: string }[]>("/api/rules/playback-users?limit=500"),
      get_api<Record<string, { instances?: { enabled: boolean }[] }>>(
        "/api/settings/services",
      ),
    ]);
    const [presetsResult, usersResult, servicesResult] = results;
    if (presetsResult.status === "fulfilled") catalog = presetsResult.value;
    else
      error = "Could not load rule presets. Close this picker and try again.";
    if (usersResult.status === "fulfilled") users = usersResult.value;
    else usersError = "Could not load users. Close this picker and try again.";
    if (servicesResult.status === "fulfilled")
      availableServices = Object.entries(servicesResult.value)
        .filter(([, config]) =>
          config?.instances?.some((instance) => instance.enabled),
        )
        .map(([service]) => service);
    else
      servicesError =
        "Could not check integrations. Close this picker and try again.";
    loading = false;
  });
</script>

<Dialog.Root
  open={true}
  onOpenChange={(open) => {
    if (!open) onCancel();
  }}
>
  <Dialog.Content class="sm:max-w-3xl max-h-[90vh] overflow-y-auto">
    <Dialog.Header>
      <Dialog.Title>Add from preset</Dialog.Title>
      <Dialog.Description
        >Choose a starting point, configure it, then preview in the rule editor.
        Nothing is saved here.</Dialog.Description
      >
    </Dialog.Header>
    {#if loading}
      <div class="flex justify-center p-6"><Spinner class="size-8" /></div>
    {:else}
      {#if error}<Notice type="error" title="Preset unavailable">{error}</Notice
        >{/if}
      {#if !selected}
        <div class="grid gap-3 sm:grid-cols-2">
          {#each catalog as preset (preset.id)}
            <button
              type="button"
              class="rounded-lg border border-border bg-card p-4 text-left hover:border-primary focus-visible:outline-2 focus-visible:outline-primary cursor-pointer"
              onclick={() => selectPreset(preset)}
            >
              <span class="block font-semibold text-foreground"
                >{preset.title}</span
              >
              <span class="block text-xs font-medium text-primary mt-1"
                >{preset.rule.action?.outcome === "protect"
                  ? "Protection"
                  : "Cleanup candidates"}</span
              >
              <span class="mt-1 block text-sm text-muted-foreground"
                >{preset.description}</span
              >
            </button>
          {/each}
        </div>
      {:else}
        <div class="space-y-4">
          <div>
            <h3 class="font-semibold">{selected.title}</h3>
            <p class="text-sm text-muted-foreground">{selected.description}</p>
            {#each selected.prerequisites as prerequisite}
              <p class="mt-2 text-sm text-muted-foreground">{prerequisite}</p>
            {/each}
          </div>
          {#if selected.target_scopes.length > 1}
            <div class="space-y-2">
              <label for="preset-target" class="block font-medium"
                >Protect</label
              >
              <select
                id="preset-target"
                class="w-full rounded-md border border-border bg-card p-2 text-sm"
                bind:value={targetScope}
                onchange={() => (libraryIds = [])}
              >
                {#each selected.target_scopes as scope}
                  <option value={scope}
                    >{scope === "movie_version"
                      ? "Movies"
                      : "Whole shows"}</option
                  >
                {/each}
              </select>
            </div>
          {/if}
          {#each missingServices as service}
            <Notice
              type="warning"
              title={`${service === "sonarr" ? "Sonarr" : "Seerr"} needed`}
            >
              {servicesError ||
                `Configure, enable, and sync ${service === "sonarr" ? "Sonarr" : "Seerr"} before using this preset.`}
            </Notice>
          {/each}
          <fieldset class="space-y-2">
            <legend class="mb-2 font-medium">Libraries (required)</legend>
            {#each matchingLibraries as library (`${library.serviceConfigId}:${library.libraryId}`)}
              <label class="flex items-center gap-2 text-sm cursor-pointer">
                <Checkbox
                  checked={libraryIds.includes(library.libraryId)}
                  onCheckedChange={(checked) => {
                    libraryIds = checked
                      ? [...libraryIds, library.libraryId]
                      : libraryIds.filter((id) => id !== library.libraryId);
                  }}
                />
                {library.libraryName}
                <span class="text-muted-foreground"
                  >({library.serviceName || library.serviceType})</span
                >
              </label>
            {:else}
              <Notice type="warning" title="Library needed"
                >Configure and sync a matching library on your main media server
                before using this preset.</Notice
              >
            {/each}
          </fieldset>
          {#if needsUsers}
            <fieldset class="space-y-2">
              <legend class="mb-2 font-medium"
                >Users who must all finish {targetScope === "series"
                  ? "the show"
                  : "the season"} (required)</legend
              >
              {#if usersError}
                <Notice type="error" title="Users unavailable"
                  >{usersError}</Notice
                >
              {:else if users.length === 0}
                <Notice type="warning" title="Watch data needed"
                  >Sync per-user watch data first. No users with imported watch
                  data are available.</Notice
                >
              {:else}
                <div class="max-h-40 overflow-y-auto space-y-2">
                  {#each users as user (user.username)}
                    <label
                      class="flex items-center gap-2 text-sm cursor-pointer"
                    >
                      <Checkbox
                        checked={usernames.includes(user.username)}
                        onCheckedChange={(checked) => {
                          usernames = checked
                            ? [...usernames, user.username]
                            : usernames.filter(
                                (name) => name !== user.username,
                              );
                        }}
                      />
                      {user.username}
                    </label>
                  {/each}
                </div>
              {/if}
            </fieldset>
          {/if}
          {#if selected.rule.action?.outcome === "protect"}
            <Notice type="info" title="Protection starts disabled"
              >Enable the saved rule to protect matching media on the next scan.
              This preset creates protections, not cleanup candidates. Review
              matches before saving.</Notice
            >
          {:else}
            <Notice type="info" title="Starts disabled"
              >The draft starts with the rule, automatic deletion, moves, and
              Arr tagging off. Enabling the rule generates candidates; automatic
              deletion is a separate choice. Review the action and preview
              matches before saving.</Notice
            >
          {/if}
        </div>
      {/if}
    {/if}
    <Dialog.Footer>
      {#if selected}<Button
          variant="secondary"
          onclick={() => {
            selected = null;
            error = "";
          }}>Back to presets</Button
        >{/if}
      <Button variant="outline" onclick={onCancel}>Cancel</Button>
      {#if selected}<Button disabled={!ready} onclick={openDraft}
          >Open in editor</Button
        >{/if}
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
