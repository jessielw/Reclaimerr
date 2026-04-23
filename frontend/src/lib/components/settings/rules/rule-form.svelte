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
  import { get_api, post_api } from "$lib/api";
  import Save from "@lucide/svelte/icons/save";
  import X from "@lucide/svelte/icons/x";
  import Info from "@lucide/svelte/icons/info";
  import Plus from "@lucide/svelte/icons/plus";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import ArrowLeft from "@lucide/svelte/icons/arrow-left";
  import FolderIcon from "@lucide/svelte/icons/folder";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import JellyfinSVG from "$lib/components/svgs/JellyfinSVG.svelte";
  import PlexSVG from "$lib/components/svgs/PlexSVG.svelte";
  import { toast } from "svelte-sonner";

  type PathNode = {
    path: string;
    name: string;
    children: PathNode[];
  };

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
    paths: null,
    series_status: null,
  });

  let selectedLibraries = $state<string[]>([]);
  let validationMessage = $state<string | null>(null);

  // media type options for select
  const mediaTypes = [
    { value: MediaType.Movie, label: "Movies" },
    { value: MediaType.Series, label: "Series" },
  ];

  const mediaTypeTriggerContent = $derived(
    mediaTypes.find((m) => m.value === formData.media_type)?.label ??
      "Select media type",
  );

  // series status options for select
  const seriesStatusOptions = [
    { value: "Returning Series", label: "Returning Series" },
    { value: "Planned", label: "Planned" },
    { value: "In Production", label: "In Production" },
    { value: "Ended", label: "Ended" },
    { value: "Canceled", label: "Canceled" },
    { value: "Pilot", label: "Pilot" },
  ];

  // Derived state for multi-select binding - handles null/undefined conversion
  let seriesStatusSelectValue = $state<string[]>([]);

  // Sync select value with form data
  $effect(() => {
    seriesStatusSelectValue = formData.series_status ?? [];
  });

  $effect(() => {
    formData.series_status =
      seriesStatusSelectValue.length > 0 ? seriesStatusSelectValue : null;
  });

  // filter libraries based on selected media type
  const filteredLibraries = $derived(
    libraries.filter((lib) => lib.mediaType === formData.media_type),
  );

  // path tree navigation state
  let pathTree = $state<PathNode[]>([]);
  let pathTreeLoading = $state(false);
  let pathTreeError = $state<string | null>(null);
  let currentPathSelection = $state<string>("");
  let pathSuffixInput = $state<string>("");
  let pathSuffixError = $state<string | null>(null);
  let validating = $state(false);

  const loadPathTree = async () => {
    if (!formData.media_type) return;
    pathTreeLoading = true;
    pathTreeError = null;
    try {
      const params = new URLSearchParams();
      params.set("media_type", formData.media_type);
      for (const id of selectedLibraries) {
        params.append("library_ids", id);
      }
      pathTree = await get_api<PathNode[]>(
        `/api/rules/path-tree?${params.toString()}`,
      );
      currentPathSelection = "";
    } catch (err: any) {
      pathTreeError = err.message ?? "Failed to load path tree";
      pathTree = [];
    } finally {
      pathTreeLoading = false;
    }
  };

  // find a node by its path within the tree
  const findNode = (nodes: PathNode[], target: string): PathNode | null => {
    for (const node of nodes) {
      if (node.path === target) return node;
      if (target.startsWith(node.path + "/")) {
        const found = findNode(node.children, target);
        if (found) return found;
      }
    }
    return null;
  };

  // children to display at the current breadcrumb level
  const currentChildren = $derived.by<PathNode[]>(() => {
    if (!currentPathSelection) return pathTree;
    const node = findNode(pathTree, currentPathSelection);
    return node ? node.children : [];
  });

  // breadcrumb segments for the current navigation path
  const breadcrumb = $derived.by<{ label: string; path: string }[]>(() => {
    if (!currentPathSelection) return [];
    // walk the tree to build the breadcrumb
    const crumbs: { label: string; path: string }[] = [];
    let nodes = pathTree;
    let remaining = currentPathSelection;
    while (remaining) {
      const match = nodes.find(
        (n) => n.path === remaining || remaining.startsWith(n.path + "/"),
      );
      if (!match) break;
      crumbs.push({ label: match.name, path: match.path });
      if (match.path === remaining) break;
      nodes = match.children;
    }
    return crumbs;
  });

  const navigateInto = (node: PathNode) => {
    currentPathSelection = node.path;
  };

  const navigateUp = () => {
    if (!currentPathSelection) return;
    const idx = currentPathSelection.lastIndexOf("/");
    currentPathSelection = idx <= 0 ? "" : currentPathSelection.slice(0, idx);
  };

  const navigateToBreadcrumb = (path: string) => {
    currentPathSelection = path;
  };

  const joinPathAndSuffix = (base: string, suffix: string): string => {
    const trimmedBase = base.replace(/[\\/]+$/, "");
    const trimmedSuffix = suffix.trim().replace(/^[\\/]+/, "");
    if (!trimmedSuffix) return trimmedBase;
    const sep =
      trimmedBase.includes("\\") && !trimmedBase.includes("/") ? "\\" : "/";
    return `${trimmedBase}${sep}${trimmedSuffix}`;
  };

  const validateRegexOnBackend = async (
    basePath: string,
    suffix: string,
  ): Promise<{
    valid: boolean;
    error: string | null;
    pattern: string | null;
  }> => {
    try {
      const response = await post_api<{
        valid: boolean;
        error: string | null;
        pattern: string | null;
      }>("/api/rules/validate-regex", { base_path: basePath, suffix: suffix });
      return response;
    } catch (e: any) {
      return {
        valid: false,
        error: e.message ?? "Validation failed",
        pattern: null,
      };
    }
  };

  const testPathCriteria = async () => {
    if (!currentPathSelection) {
      validatedPattern = null;
      return;
    }

    validating = true;
    const validation = await validateRegexOnBackend(
      currentPathSelection,
      pathSuffixInput ?? "",
    );
    validating = false;

    if (validation.valid) {
      pathSuffixError = null;
      validatedPattern = validation.pattern;
    } else {
      pathSuffixError = validation.error;
      validatedPattern = null;
    }
  };

  let validatedPattern = $state<string | null>(null);

  // Trigger validation when path selection changes
  $effect(() => {
    if (currentPathSelection) {
      void testPathCriteria();
    } else {
      validatedPattern = null;
      pathSuffixError = null;
    }
  });

  const addSelectedPath = async () => {
    if (!currentPathSelection) {
      toast.error("Navigate into a folder first.");
      return;
    }

    // Validate and get the complete pattern from backend
    validating = true;
    const validation = await validateRegexOnBackend(
      currentPathSelection,
      pathSuffixInput,
    );
    validating = false;

    if (!validation.valid) {
      pathSuffixError = validation.error ?? "Invalid regex pattern";
      toast.error(pathSuffixError);
      validatedPattern = null;
      return;
    }

    // Use the pattern returned by backend
    const finalPattern = validation.pattern;
    if (!finalPattern) {
      toast.error("Failed to construct pattern");
      validatedPattern = null;
      return;
    }

    const existing = formData.paths ?? [];
    if (existing.includes(finalPattern)) {
      toast.error("That path is already in the list.");
      validatedPattern = null;
      return;
    }
    formData.paths = [...existing, finalPattern];
    pathSuffixInput = "";
    pathSuffixError = null;
    validatedPattern = null;
  };

  const removePath = (path: string) => {
    const next = (formData.paths ?? []).filter((p) => p !== path);
    formData.paths = next.length > 0 ? next : null;
  };

  // reload the tree whenever the rule's media type or library selection changes
  $effect(() => {
    void formData.media_type;
    void selectedLibraries;
    loadPathTree();
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
        paths: rule?.paths ? [...rule.paths] : null,
        series_status: rule?.series_status ? [...rule.series_status] : null,
      };
      selectedLibraries = rule?.library_ids ? [...rule.library_ids] : [];
      validationMessage = null;
      currentPathSelection = "";
      pathSuffixInput = "";
    }
  });
  let saving = $state(false);

  const hasConfiguredCriteria = (ruleData: Partial<ReclaimRule>) =>
    ruleData.min_popularity !== null ||
    ruleData.max_popularity !== null ||
    ruleData.min_vote_average !== null ||
    ruleData.max_vote_average !== null ||
    ruleData.min_vote_count !== null ||
    ruleData.max_vote_count !== null ||
    ruleData.min_view_count !== null ||
    ruleData.max_view_count !== null ||
    ruleData.include_never_watched ||
    ruleData.min_days_since_added !== null ||
    ruleData.max_days_since_added !== null ||
    ruleData.min_days_since_last_watched !== null ||
    ruleData.max_days_since_last_watched !== null ||
    ruleData.min_size !== null ||
    ruleData.max_size !== null ||
    (ruleData.series_status !== undefined &&
      ruleData.series_status !== null &&
      ruleData.series_status.length > 0) ||
    (ruleData.paths !== null &&
      ruleData.paths !== undefined &&
      ruleData.paths.length > 0);

  const updateLibrarySelection = (libraryId: string, selected: boolean) => {
    if (selected) {
      selectedLibraries = [...selectedLibraries, libraryId];
    } else {
      selectedLibraries = selectedLibraries.filter((id) => id !== libraryId);
    }
    formData.library_ids =
      selectedLibraries.length > 0 ? selectedLibraries : null;
  };

  const handleSubmit = async (e: Event) => {
    e.preventDefault();
    validationMessage = null;

    if (!formData.name || formData.name.trim() === "") {
      validationMessage = "Please enter a rule name.";
      toast.error(validationMessage);
      return;
    }

    if (!hasConfiguredCriteria(formData)) {
      validationMessage =
        "Add at least one rule condition before saving. Library selection alone is not enough.";
      toast.error(validationMessage);
      return;
    }

    try {
      saving = true;
      await onSave(formData);
    } finally {
      saving = false;
    }
  };

  const formatBytes = (bytes: number | null): string => {
    if (bytes === null) return "";
    const gb = bytes / (1024 * 1024 * 1024);
    return gb.toFixed(2);
  };

  const parseBytes = (gb: string): number | null => {
    if (!gb || gb.trim() === "") return null;
    const value = parseFloat(gb);
    if (isNaN(value)) return null;
    return Math.floor(value * 1024 * 1024 * 1024);
  };
</script>

<div
  class="fixed inset-0 bg-black/50 z-40 flex items-center justify-center overflow-y-auto"
>
  <div class="bg-card rounded-lg border border-border max-w-4xl w-full">
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
        {#if validationMessage}
          <p
            class="mt-3 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm
            text-destructive"
          >
            {validationMessage}
          </p>
        {/if}
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
                Select which libraries this rule applies to (leave empty for
                all)
              </Card.Description>
            </Card.Header>
            <Card.Content>
              <div class="space-y-2">
                {#each filteredLibraries as library}
                  <div class="flex items-center space-x-2">
                    <Switch
                      id={`library-${library.libraryId}`}
                      checked={selectedLibraries.includes(library.libraryId)}
                      onCheckedChange={(checked) =>
                        updateLibrarySelection(library.libraryId, checked)}
                      class="cursor-pointer"
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
                        class="cursor-pointer"
                      >
                        {library.libraryName}
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
            <!-- popularity -->
            <div class="grid grid-cols-2 gap-4">
              <div class="space-y-2">
                <div class="flex items-center gap-1">
                  <Label for="min-popularity" class="m-0 p-0"
                    >Min Popularity</Label
                  >
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Info class="size-4 text-muted-foreground cursor-help" />
                    </Tooltip.Trigger>
                    <Tooltip.Content>
                      <p>TMDB popularity score (higher = more popular)</p>
                    </Tooltip.Content>
                  </Tooltip.Root>
                </div>
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
                <div class="flex items-center gap-1">
                  <Label for="max-popularity" class="m-0 p-0"
                    >Max Popularity</Label
                  >
                  <!-- empty span to match the icon's space for alignment -->
                  <span class="inline-block w-4 h-4"></span>
                </div>
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

            <!-- rating -->
            <div class="grid grid-cols-2 gap-4">
              <div class="space-y-2">
                <div class="flex items-center gap-1">
                  <Label for="min-vote-avg" class="m-0 p-0">Min Rating</Label>
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Info class="size-4 text-muted-foreground cursor-help" />
                    </Tooltip.Trigger>
                    <Tooltip.Content>
                      <p>TMDB vote average (0-10)</p>
                    </Tooltip.Content>
                  </Tooltip.Root>
                </div>
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
                <div class="flex items-center gap-1">
                  <Label for="max-vote-avg" class="m-0 p-0">Max Rating</Label>
                  <!-- empty span to match the icon's space for alignment -->
                  <span class="inline-block w-4 h-4"></span>
                </div>
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

            <!-- vote count -->
            <div class="grid grid-cols-2 gap-4">
              <div class="space-y-2">
                <div class="flex items-center gap-1">
                  <Label for="min-vote-count" class="m-0 p-0"
                    >Min Vote Count</Label
                  >
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Info class="size-4 text-muted-foreground cursor-help" />
                    </Tooltip.Trigger>
                    <Tooltip.Content>
                      <p>Minimum number of TMDB votes</p>
                    </Tooltip.Content>
                  </Tooltip.Root>
                </div>
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
                <div class="flex items-center gap-1">
                  <Label for="max-vote-count" class="m-0 p-0"
                    >Max Vote Count</Label
                  >
                  <!-- empty span to match the icon's space for alignment -->
                  <span class="inline-block w-4 h-4"></span>
                </div>
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

            <!-- series status - only for series -->
            {#if formData.media_type === MediaType.Series}
              <div class="space-y-2">
                <Label for="series-status">Series Status</Label>
                <Select.Root
                  type="multiple"
                  name="series-status"
                  bind:value={seriesStatusSelectValue}
                >
                  <Select.Trigger class="w-full">
                    {seriesStatusSelectValue.length > 0
                      ? `${seriesStatusSelectValue.length} selected`
                      : "Any Status"}
                  </Select.Trigger>
                  <Select.Content>
                    {#each seriesStatusOptions as option}
                      <Select.Item value={option.value} label={option.label}>
                        {option.label}
                      </Select.Item>
                    {/each}
                  </Select.Content>
                </Select.Root>
              </div>
            {/if}
          </Card.Content>
        </Card.Root>

        <!-- watch history criteria -->
        <Card.Root>
          <Card.Header>
            <Card.Title>Watch History Criteria</Card.Title>
            <Card.Description>
              Filter by view counts and watch patterns (all users)
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
                <div class="flex items-center gap-1">
                  <Label for="min-days-added" class="m-0 p-0"
                    >Min Days Since Added</Label
                  >
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Info class="size-4 text-muted-foreground cursor-help" />
                    </Tooltip.Trigger>
                    <Tooltip.Content>
                      <p>How many days ago it was added (minimum)</p>
                    </Tooltip.Content>
                  </Tooltip.Root>
                </div>
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
                <div class="flex items-center gap-1">
                  <Label for="max-days-added" class="m-0 p-0"
                    >Max Days Since Added</Label
                  >
                  <!-- empty span to match the icon's space for alignment -->
                  <span class="inline-block w-4 h-4"></span>
                </div>
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
                <div class="flex items-center gap-1">
                  <Label for="min-days-watched" class="m-0 p-0"
                    >Min Days Since Last Watched</Label
                  >
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Info class="size-4 text-muted-foreground cursor-help" />
                    </Tooltip.Trigger>
                    <Tooltip.Content>
                      <p>How long since it was last watched (minimum)</p>
                    </Tooltip.Content>
                  </Tooltip.Root>
                </div>
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
                <div class="flex items-center gap-1">
                  <Label for="max-days-watched" class="m-0 p-0"
                    >Max Days Since Last Watched</Label
                  >
                  <!-- empty span to match the icon's space for alignment -->
                  <span class="inline-block w-4 h-4"></span>
                </div>
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

        <!-- path criteria -->
        <Card.Root>
          <Card.Header>
            <Card.Title class="flex items-center gap-1">
              Path Criteria
              <Tooltip.Root>
                <Tooltip.Trigger>
                  <Info class="size-4 text-muted-foreground cursor-help" />
                </Tooltip.Trigger>
                <Tooltip.Content>
                  <p>
                    Only media stored under one of these paths will be
                    considered. Suffix supports regex patterns (e.g.,
                    <code>.*1080p.*</code> or <code>.*/Extras/.*</code>).
                  </p>
                </Tooltip.Content>
              </Tooltip.Root>
            </Card.Title>
            <Card.Description>
              Restrict matches to specific folders under your library roots
            </Card.Description>
          </Card.Header>
          <Card.Content class="space-y-4">
            {#if pathTreeLoading}
              <p class="text-sm text-muted-foreground">Loading paths…</p>
            {:else if pathTreeError}
              <p class="text-sm text-destructive">{pathTreeError}</p>
            {:else if pathTree.length === 0}
              <p class="text-sm text-muted-foreground">
                No indexed media paths are available yet. Run a media sync
                before adding path criteria.
              </p>
            {:else}
              <!-- breadcrumb -->
              <div class="flex items-center gap-1 flex-wrap text-sm">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onclick={() => (currentPathSelection = "")}
                  class="cursor-pointer h-7 px-2"
                  disabled={!currentPathSelection}
                >
                  <FolderIcon class="size-4 mr-1" />
                  Roots
                </Button>
                {#each breadcrumb as crumb, i}
                  <ChevronRight class="size-3 text-muted-foreground" />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onclick={() => navigateToBreadcrumb(crumb.path)}
                    disabled={i === breadcrumb.length - 1}
                    class="cursor-pointer h-7 px-2 font-mono"
                  >
                    {crumb.label}
                  </Button>
                {/each}
                {#if currentPathSelection}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onclick={navigateUp}
                    class="cursor-pointer h-7 px-2 ml-auto gap-1"
                  >
                    <ArrowLeft class="size-4" />
                    Up
                  </Button>
                {/if}
              </div>

              <!-- folder list -->
              <div
                class="rounded-md border border-border divide-y divide-border max-h-64 overflow-y-auto"
              >
                {#if currentChildren.length === 0}
                  {#if currentPathSelection}
                    <div class="p-3 text-sm text-muted-foreground">
                      Use the wildcard suffix field below to further restrict
                      paths (e.g., *1080p* or **/Extras/*)
                    </div>
                  {/if}
                {:else}
                  {#each currentChildren as node}
                    <div
                      class="flex items-center justify-between gap-2 px-3 py-2 hover:bg-muted/40 cursor-pointer"
                      role="button"
                      tabindex="0"
                      onclick={() => navigateInto(node)}
                      onkeydown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          navigateInto(node);
                        }
                      }}
                    >
                      <div class="flex items-center gap-2 flex-1 min-w-0">
                        <FolderIcon
                          class="size-4 shrink-0 text-muted-foreground"
                        />
                        <span class="font-mono text-sm truncate">
                          {node.name}
                        </span>
                      </div>
                      {#if node.children.length > 0}
                        <ChevronRight
                          class="size-4 text-muted-foreground shrink-0"
                        />
                      {/if}
                    </div>
                  {/each}
                {/if}
              </div>

              <!-- current selection + suffix + add -->
              <div class="space-y-3">
                <div class="space-y-2">
                  <Label for="path-suffix">
                    Optional Regex Suffix (applied to current folder)
                  </Label>
                  <div class="flex gap-2">
                    {#if !currentPathSelection}
                      <Tooltip.Root>
                        <Tooltip.Trigger class="flex-1">
                          <Input
                            id="path-suffix"
                            type="text"
                            placeholder="e.g. .*1080p.* or .*/Extras/.*"
                            bind:value={pathSuffixInput}
                            disabled={!currentPathSelection}
                            class={pathSuffixError ? "border-destructive" : ""}
                          />
                        </Tooltip.Trigger>
                        <Tooltip.Content>
                          <p>Navigate into a folder first to add a suffix.</p>
                        </Tooltip.Content>
                      </Tooltip.Root>
                    {:else}
                      <Input
                        id="path-suffix"
                        type="text"
                        placeholder="e.g. .*1080p.* or .*/Extras/.*"
                        bind:value={pathSuffixInput}
                        disabled={!currentPathSelection}
                        class={pathSuffixError ? "border-destructive" : ""}
                      />
                    {/if}
                  </div>
                  {#if pathSuffixError}
                    <p class="text-xs text-destructive">{pathSuffixError}</p>
                  {/if}
                </div>
                <div class="flex gap-2">
                  <Label for="pattern-display" class="flex items-center"
                    >Pattern</Label
                  >
                  <div class="flex-1 flex gap-2">
                    <div
                      class="flex-1 rounded-md border border-input bg-muted px-3 py-2 text-sm font-mono"
                    >
                      {validatedPattern ||
                        joinPathAndSuffix(
                          currentPathSelection,
                          pathSuffixInput,
                        )}
                    </div>
                    <Button
                      type="button"
                      variant="secondary"
                      onclick={addSelectedPath}
                      disabled={!currentPathSelection ||
                        !!pathSuffixError ||
                        validating}
                      class="cursor-pointer shrink-0"
                    >
                      {#if validating}
                        <span class="animate-spin">⏳</span>
                      {:else}
                        <Plus class="size-4" />
                      {/if}
                    </Button>
                  </div>
                </div>
              </div>
            {/if}

            {#if formData.paths && formData.paths.length > 0}
              <div class="space-y-2">
                <Label>Configured Paths</Label>
                <ul class="space-y-2">
                  {#each formData.paths as path}
                    <li
                      class="flex items-center justify-between gap-2 rounded-md border border-border bg-muted/30 px-3 py-2"
                    >
                      <span class="font-mono text-sm break-all">{path}</span>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onclick={() => removePath(path)}
                        class="cursor-pointer"
                      >
                        <Trash2 class="size-4" />
                      </Button>
                    </li>
                  {/each}
                </ul>
              </div>
            {/if}
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
      </div>

      <div
        class="p-6 border-t border-border flex flex-col md:flex-row items-center justify-end gap-3"
      >
        <p class="text-xs text-foreground mr-3 mt-1 md:text-left">
          <strong>Note:</strong> New candidates will appear next time the Scan Cleanup
          Candidates task is run. If you want them sooner, you can manually trigger
          the scan.
        </p>
        <div class="flex justify-end items-center gap-2">
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
      </div>
    </form>
  </div>
</div>
