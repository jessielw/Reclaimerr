<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import * as Card from "$lib/components/ui/card/index.js";
  import {
    MediaType,
    SettingsTab,
    type ReclaimRule,
    type LibraryType,
  } from "$lib/types/shared";
  import Save from "@lucide/svelte/icons/save";
  import X from "@lucide/svelte/icons/x";
  import Info from "@lucide/svelte/icons/info";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import JellyfinSVG from "$lib/components/svgs/JellyfinSVG.svelte";
  import PlexSVG from "$lib/components/svgs/PlexSVG.svelte";

  // props
  let {
    rule = null,
    libraries = [],
    onSave,
    onCancel,
  }: {
    rule: ReclaimRule | null;
    libraries: LibraryType[];
    onSave: (rule: Partial<ReclaimRule>) => Promise<void>;
    onCancel: () => void;
  } = $props();

  // form state - initialize with defaults
  let formData = $state<Partial<ReclaimRule>>({
    name: "",
    media_type: MediaType.Movie,
    enabled: true,
    library_ids: null,
    min_popularity: null,
    max_popularity: null,
    min_vote_average: null,
    max_vote_average: null,
    min_vote_count: null,
    max_vote_count: null,
    min_view_count: null,
    max_view_count: null,
    include_never_watched: false,
    min_days_since_added: null,
    max_days_since_added: null,
    min_days_since_last_watched: null,
    max_days_since_last_watched: null,
    min_size: null,
    max_size: null,
    auto_tag: true,
  });

  let selectedLibraries = $state<string[]>([]);

  // media type options for select
  const mediaTypes = [
    { value: MediaType.Movie, label: "Movies" },
    { value: MediaType.Series, label: "Series" },
  ];

  const mediaTypeTriggerContent = $derived(
    mediaTypes.find((m) => m.value === formData.media_type)?.label ??
      "Select media type",
  );

  // filter libraries based on selected media type
  // show all libraries (both selected and unselected) but unselected will be disabled
  const filteredLibraries = $derived(
    libraries.filter((lib) => lib.mediaType === formData.media_type),
  );

  // check if rule has any library_ids that reference unselected libraries
  const hasDisabledLibraries = $derived(() => {
    if (!selectedLibraries.length) return false;
    const selectedLibraryIds = libraries
      .filter((lib) => lib.selected && lib.mediaType === formData.media_type)
      .map((lib) => lib.libraryId);
    return selectedLibraries.some((id) => !selectedLibraryIds.includes(id));
  });

  // reset form when rule changes
  $effect(() => {
    if (rule !== undefined) {
      formData = {
        name: rule?.name || "",
        media_type: rule?.media_type || MediaType.Movie,
        enabled: rule?.enabled ?? true,
        library_ids: rule?.library_ids || null,
        min_popularity: rule?.min_popularity ?? null,
        max_popularity: rule?.max_popularity ?? null,
        min_vote_average: rule?.min_vote_average ?? null,
        max_vote_average: rule?.max_vote_average ?? null,
        min_vote_count: rule?.min_vote_count ?? null,
        max_vote_count: rule?.max_vote_count ?? null,
        min_view_count: rule?.min_view_count ?? null,
        max_view_count: rule?.max_view_count ?? null,
        include_never_watched: rule?.include_never_watched ?? false,
        min_days_since_added: rule?.min_days_since_added ?? null,
        max_days_since_added: rule?.max_days_since_added ?? null,
        min_days_since_last_watched: rule?.min_days_since_last_watched ?? null,
        max_days_since_last_watched: rule?.max_days_since_last_watched ?? null,
        min_size: rule?.min_size ?? null,
        max_size: rule?.max_size ?? null,
        auto_tag: rule?.auto_tag ?? true,
      };
      selectedLibraries = rule?.library_ids ? [...rule.library_ids] : [];
    }
  });
  let saving = $state(false);

  function updateLibrarySelection(libraryId: string, selected: boolean) {
    if (selected) {
      selectedLibraries = [...selectedLibraries, libraryId];
    } else {
      selectedLibraries = selectedLibraries.filter((id) => id !== libraryId);
    }
    formData.library_ids =
      selectedLibraries.length > 0 ? selectedLibraries : null;
  }

  async function handleSubmit(e: Event) {
    e.preventDefault();

    if (!formData.name || formData.name.trim() === "") {
      alert("Please enter a rule name");
      return;
    }

    // clean up any library_ids that reference disabled libraries
    if (formData.library_ids && formData.library_ids.length > 0) {
      const enabledLibraryIds = libraries
        .filter((lib) => lib.selected && lib.mediaType === formData.media_type)
        .map((lib) => lib.libraryId);

      const cleanedLibraryIds = formData.library_ids.filter((id) =>
        enabledLibraryIds.includes(id),
      );

      formData.library_ids =
        cleanedLibraryIds.length > 0 ? cleanedLibraryIds : null;
      selectedLibraries = cleanedLibraryIds;
    }

    try {
      saving = true;
      await onSave(formData);
    } finally {
      saving = false;
    }
  }

  function formatBytes(bytes: number | null): string {
    if (bytes === null) return "";
    const gb = bytes / (1024 * 1024 * 1024);
    return gb.toFixed(2);
  }

  function parseBytes(gb: string): number | null {
    if (!gb || gb.trim() === "") return null;
    const value = parseFloat(gb);
    if (isNaN(value)) return null;
    return Math.floor(value * 1024 * 1024 * 1024);
  }
</script>

<div
  class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4 overflow-y-auto"
>
  <div class="bg-card rounded-lg border border-border max-w-4xl w-full my-8">
    <form onsubmit={handleSubmit}>
      <div class="p-6 border-b border-border">
        <div class="flex items-center justify-between">
          <h2 class="text-2xl font-bold text-foreground">
            {rule ? "Edit Rule" : "Create New Rule"}
          </h2>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onclick={onCancel}
            class="cursor-pointer"
          >
            <X class="size-5" />
          </Button>
        </div>
        <p class="text-muted-foreground mt-1">
          Define conditions to identify media candidates for cleanup
        </p>
      </div>

      <div class="p-6 space-y-6 max-h-[70vh] overflow-y-auto">
        <!-- settings -->
        <Card.Root>
          <Card.Header>
            <Card.Title class="flex justify-between">
              Basic Settings
              <!-- toggle -->
              <div class="flex items-center space-x-2">
                <Switch
                  id="enabled"
                  checked={formData.enabled}
                  onCheckedChange={(checked) => (formData.enabled = checked)}
                  class="cursor-pointer"
                />
                <Label for="enabled">Enabled</Label>
              </div>
            </Card.Title>
            <Card.Description>
              Name, media type, and enabled status
            </Card.Description>
          </Card.Header>
          <Card.Content class="space-y-4">
            <!-- rule name -->
            <div class="space-y-2">
              <Label for="rule-name">Rule Name</Label>
              <Input
                id="rule-name"
                type="text"
                placeholder="e.g., Low-rated old movies"
                bind:value={formData.name}
                required
              />
            </div>

            <!-- media type -->
            <div class="space-y-2 max-w-xs">
              <Label for="media-type">Media Type</Label>
              <Select.Root
                type="single"
                name="media-type"
                bind:value={formData.media_type}
              >
                <Select.Trigger class="w-full">
                  {mediaTypeTriggerContent}
                </Select.Trigger>
                <Select.Content>
                  {#each mediaTypes as mediaType (mediaType.value)}
                    <Select.Item
                      value={mediaType.value}
                      label={mediaType.label}
                    >
                      {mediaType.label}
                    </Select.Item>
                  {/each}
                </Select.Content>
              </Select.Root>
            </div>
          </Card.Content>
        </Card.Root>

        <!-- Library Selection -->
        {#if filteredLibraries.length > 0}
          <Card.Root>
            <Card.Header>
              <Card.Title>Libraries</Card.Title>
              <Card.Description>
                Select specific libraries (leave empty for all)
              </Card.Description>
            </Card.Header>
            <Card.Content>
              {#if hasDisabledLibraries()}
                <div
                  class="mb-4 p-3 rounded-md bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800"
                >
                  <p class="text-sm text-yellow-800 dark:text-yellow-200">
                    <strong>Warning:</strong> This rule references libraries that
                    are disabled in Settings. They will be removed when you save this
                    rule.
                  </p>
                </div>
              {/if}
              <div class="space-y-2">
                {#each filteredLibraries as library}
                  <div
                    class="flex items-center space-x-2"
                    class:opacity-50={!library.selected}
                  >
                    <Switch
                      id={`library-${library.libraryId}`}
                      checked={selectedLibraries.includes(library.libraryId)}
                      disabled={!library.selected}
                      onCheckedChange={(checked) =>
                        updateLibrarySelection(library.libraryId, checked)}
                      class={library.selected
                        ? "cursor-pointer"
                        : "cursor-not-allowed"}
                    />
                    <div class="flex items-center space-x-1.5">
                      <div class="w-4 h-4 shrink-0">
                        {#if library.serviceType === SettingsTab.Jellyfin}
                          <JellyfinSVG />
                        {:else if library.serviceType === SettingsTab.Plex}
                          <PlexSVG />
                        {/if}
                      </div>
                      <Label
                        for={`library-${library.libraryId}`}
                        class={library.selected
                          ? "cursor-pointer"
                          : "cursor-not-allowed"}
                      >
                        {library.libraryName}
                        {#if !library.selected}
                          <Tooltip.Root>
                            <Tooltip.Trigger>
                              <Info
                                class="inline size-4 ml-1 text-muted-foreground cursor-help"
                              />
                            </Tooltip.Trigger>
                            <Tooltip.Content>
                              <p>This library is not enabled in Settings</p>
                            </Tooltip.Content>
                          </Tooltip.Root>
                        {/if}
                      </Label>
                    </div>
                  </div>
                {/each}
              </div>
            </Card.Content>
          </Card.Root>
        {/if}

        <!-- TMDB criteria -->
        <Card.Root>
          <Card.Header>
            <Card.Title>TMDB Criteria</Card.Title>
            <Card.Description>
              Filter by popularity, rating, and vote count
            </Card.Description>
          </Card.Header>
          <Card.Content class="space-y-4">
            <div class="grid grid-cols-2 gap-4">
              <div class="space-y-2">
                <Label for="min-popularity">
                  Min Popularity
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Info
                        class="inline size-4 ml-1 text-muted-foreground cursor-help"
                      />
                    </Tooltip.Trigger>
                    <Tooltip.Content>
                      <p>TMDB popularity score (higher = more popular)</p>
                    </Tooltip.Content>
                  </Tooltip.Root>
                </Label>
                <Input
                  id="min-popularity"
                  type="number"
                  step="0.1"
                  placeholder="e.g., 10"
                  value={formData.min_popularity ?? ""}
                  oninput={(e) =>
                    (formData.min_popularity =
                      e.currentTarget.value === ""
                        ? null
                        : parseFloat(e.currentTarget.value))}
                />
              </div>

              <div class="space-y-2">
                <Label for="max-popularity">Max Popularity</Label>
                <Input
                  id="max-popularity"
                  type="number"
                  step="0.1"
                  placeholder="e.g., 50"
                  value={formData.max_popularity ?? ""}
                  oninput={(e) =>
                    (formData.max_popularity =
                      e.currentTarget.value === ""
                        ? null
                        : parseFloat(e.currentTarget.value))}
                />
              </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div class="space-y-2">
                <Label for="min-vote-avg">
                  Min Rating
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Info
                        class="inline size-4 ml-1 text-muted-foreground cursor-help"
                      />
                    </Tooltip.Trigger>
                    <Tooltip.Content>
                      <p>TMDB vote average (0-10)</p>
                    </Tooltip.Content>
                  </Tooltip.Root>
                </Label>
                <Input
                  id="min-vote-avg"
                  type="number"
                  step="0.1"
                  min="0"
                  max="10"
                  placeholder="e.g., 5.0"
                  value={formData.min_vote_average ?? ""}
                  oninput={(e) =>
                    (formData.min_vote_average =
                      e.currentTarget.value === ""
                        ? null
                        : parseFloat(e.currentTarget.value))}
                />
              </div>

              <div class="space-y-2">
                <Label for="max-vote-avg">Max Rating</Label>
                <Input
                  id="max-vote-avg"
                  type="number"
                  step="0.1"
                  min="0"
                  max="10"
                  placeholder="e.g., 7.0"
                  value={formData.max_vote_average ?? ""}
                  oninput={(e) =>
                    (formData.max_vote_average =
                      e.currentTarget.value === ""
                        ? null
                        : parseFloat(e.currentTarget.value))}
                />
              </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div class="space-y-2">
                <Label for="min-vote-count">
                  Min Vote Count
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Info
                        class="inline size-4 ml-1 text-muted-foreground cursor-help"
                      />
                    </Tooltip.Trigger>
                    <Tooltip.Content>
                      <p>Minimum number of TMDB votes</p>
                    </Tooltip.Content>
                  </Tooltip.Root>
                </Label>
                <Input
                  id="min-vote-count"
                  type="number"
                  placeholder="e.g., 100"
                  value={formData.min_vote_count ?? ""}
                  oninput={(e) =>
                    (formData.min_vote_count =
                      e.currentTarget.value === ""
                        ? null
                        : parseInt(e.currentTarget.value))}
                />
              </div>

              <div class="space-y-2">
                <Label for="max-vote-count">Max Vote Count</Label>
                <Input
                  id="max-vote-count"
                  type="number"
                  placeholder="e.g., 1000"
                  value={formData.max_vote_count ?? ""}
                  oninput={(e) =>
                    (formData.max_vote_count =
                      e.currentTarget.value === ""
                        ? null
                        : parseInt(e.currentTarget.value))}
                />
              </div>
            </div>
          </Card.Content>
        </Card.Root>

        <!-- watch history criteria -->
        <Card.Root>
          <Card.Header>
            <Card.Title>Watch History Criteria</Card.Title>
            <Card.Description>
              Filter by view counts and watch patterns
            </Card.Description>
          </Card.Header>
          <Card.Content class="space-y-4">
            <div class="grid grid-cols-2 gap-4">
              <div class="space-y-2">
                <Label for="min-view-count">Min View Count</Label>
                <Input
                  id="min-view-count"
                  type="number"
                  placeholder="e.g., 0"
                  value={formData.min_view_count ?? ""}
                  oninput={(e) =>
                    (formData.min_view_count =
                      e.currentTarget.value === ""
                        ? null
                        : parseInt(e.currentTarget.value))}
                />
              </div>

              <div class="space-y-2">
                <Label for="max-view-count">Max View Count</Label>
                <Input
                  id="max-view-count"
                  type="number"
                  placeholder="e.g., 5"
                  value={formData.max_view_count ?? ""}
                  oninput={(e) =>
                    (formData.max_view_count =
                      e.currentTarget.value === ""
                        ? null
                        : parseInt(e.currentTarget.value))}
                />
              </div>
            </div>

            <div class="flex items-center space-x-2">
              <Switch
                id="never-watched"
                checked={formData.include_never_watched}
                onCheckedChange={(checked) =>
                  (formData.include_never_watched = checked)}
                class="cursor-pointer"
              />
              <Label for="never-watched">Include Never Watched Items</Label>
            </div>
          </Card.Content>
        </Card.Root>

        <!-- age & recency criteria -->
        <Card.Root>
          <Card.Header>
            <Card.Title>Age & Recency Criteria</Card.Title>
            <Card.Description>
              Filter by how long media has been in library
            </Card.Description>
          </Card.Header>
          <Card.Content class="space-y-4">
            <div class="grid grid-cols-2 gap-4">
              <div class="space-y-2">
                <Label for="min-days-added">
                  Min Days Since Added
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Info
                        class="inline size-4 ml-1 text-muted-foreground cursor-help"
                      />
                    </Tooltip.Trigger>
                    <Tooltip.Content>
                      <p>How many days ago it was added (minimum)</p>
                    </Tooltip.Content>
                  </Tooltip.Root>
                </Label>
                <Input
                  id="min-days-added"
                  type="number"
                  placeholder="e.g., 30"
                  value={formData.min_days_since_added ?? ""}
                  oninput={(e) =>
                    (formData.min_days_since_added =
                      e.currentTarget.value === ""
                        ? null
                        : parseInt(e.currentTarget.value))}
                />
              </div>

              <div class="space-y-2">
                <Label for="max-days-added">Max Days Since Added</Label>
                <Input
                  id="max-days-added"
                  type="number"
                  placeholder="e.g., 365"
                  value={formData.max_days_since_added ?? ""}
                  oninput={(e) =>
                    (formData.max_days_since_added =
                      e.currentTarget.value === ""
                        ? null
                        : parseInt(e.currentTarget.value))}
                />
              </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div class="space-y-2">
                <Label for="min-days-watched">
                  Min Days Since Last Watched
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Info
                        class="inline size-4 ml-1 text-muted-foreground cursor-help"
                      />
                    </Tooltip.Trigger>
                    <Tooltip.Content>
                      <p>How long since it was last watched (minimum)</p>
                    </Tooltip.Content>
                  </Tooltip.Root>
                </Label>
                <Input
                  id="min-days-watched"
                  type="number"
                  placeholder="e.g., 90"
                  value={formData.min_days_since_last_watched ?? ""}
                  oninput={(e) =>
                    (formData.min_days_since_last_watched =
                      e.currentTarget.value === ""
                        ? null
                        : parseInt(e.currentTarget.value))}
                />
              </div>

              <div class="space-y-2">
                <Label for="max-days-watched">Max Days Since Last Watched</Label
                >
                <Input
                  id="max-days-watched"
                  type="number"
                  placeholder="e.g., 180"
                  value={formData.max_days_since_last_watched ?? ""}
                  oninput={(e) =>
                    (formData.max_days_since_last_watched =
                      e.currentTarget.value === ""
                        ? null
                        : parseInt(e.currentTarget.value))}
                />
              </div>
            </div>
          </Card.Content>
        </Card.Root>

        <!-- size criteria -->
        <Card.Root>
          <Card.Header>
            <Card.Title>Size Criteria</Card.Title>
            <Card.Description>Filter by file size (in GB)</Card.Description>
          </Card.Header>
          <Card.Content>
            <div class="grid grid-cols-2 gap-4">
              <div class="space-y-2">
                <Label for="min-size">Min Size (GB)</Label>
                <Input
                  id="min-size"
                  type="number"
                  step="0.01"
                  placeholder="e.g., 1.00"
                  value={formatBytes(formData.min_size ?? null)}
                  oninput={(e) =>
                    (formData.min_size = parseBytes(e.currentTarget.value))}
                />
              </div>

              <div class="space-y-2">
                <Label for="max-size">Max Size (GB)</Label>
                <Input
                  id="max-size"
                  type="number"
                  step="0.01"
                  placeholder="e.g., 50.00"
                  value={formatBytes(formData.max_size ?? null)}
                  oninput={(e) =>
                    (formData.max_size = parseBytes(e.currentTarget.value))}
                />
              </div>
            </div>
          </Card.Content>
        </Card.Root>

        <!-- actions -->
        <Card.Root>
          <Card.Header>
            <Card.Title>Actions</Card.Title>
            <Card.Description>
              What should happen when items match this rule
            </Card.Description>
          </Card.Header>
          <Card.Content>
            <div class="flex items-center space-x-2">
              <Switch
                id="auto-tag"
                checked={formData.auto_tag}
                onCheckedChange={(checked) => (formData.auto_tag = checked)}
                class="cursor-pointer"
              />
              <Label for="auto-tag">
                Automatically Tag Matching Items
                <Tooltip.Root>
                  <Tooltip.Trigger>
                    <Info
                      class="inline size-4 ml-1 text-muted-foreground cursor-help"
                    />
                  </Tooltip.Trigger>
                  <Tooltip.Content>
                    <p>Tag items as cleanup candidates when they match</p>
                  </Tooltip.Content>
                </Tooltip.Root>
              </Label>
            </div>
          </Card.Content>
        </Card.Root>
      </div>

      <div
        class="p-6 border-t border-border flex items-center justify-end gap-3"
      >
        <Button
          type="button"
          variant="secondary"
          onclick={onCancel}
          disabled={saving}
          class="cursor-pointer"
        >
          Cancel
        </Button>
        <Button type="submit" disabled={saving} class="cursor-pointer gap-2">
          <Save class="size-4" />
          {saving ? "Saving..." : "Save Rule"}
        </Button>
      </div>
    </form>
  </div>
</div>
