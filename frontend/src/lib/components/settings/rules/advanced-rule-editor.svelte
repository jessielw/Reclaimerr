<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import { get_api, post_api } from "$lib/api";
  import { onMount } from "svelte";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import Eye from "@lucide/svelte/icons/eye";
  import CompactPagination from "$lib/components/compact-pagination.svelte";
  import Notice from "$lib/components/notice.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import JellyfinSVG from "$lib/components/svgs/jellyfin-svg.svelte";
  import PlexSVG from "$lib/components/svgs/plex-svg.svelte";
  import EmbySVG from "$lib/components/svgs/emby-svg.svelte";
  import RadarrSVG from "$lib/components/svgs/radarr-svg.svelte";
  import SonarrSVG from "$lib/components/svgs/sonarr-svg.svelte";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
  import ArrowLeft from "@lucide/svelte/icons/arrow-left";
  import Save from "@lucide/svelte/icons/save";
  import RuleNodeEditor from "$lib/components/settings/rules/rule-node-editor.svelte";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { toast } from "svelte-sonner";
  import {
    MediaType,
    SettingsTab,
    type LibraryType,
    type ReclaimRule,
    type PaginatedResponse,
    type RuleCondition,
    type RuleConditionOperator,
    type RuleDefinition,
    type RuleNode,
    type RulePreviewEntry,
  } from "$lib/types/shared";

  interface Props {
    rule: ReclaimRule | null;
    libraries: LibraryType[];
    onSave: (rule: Partial<ReclaimRule>) => Promise<void>;
    onCancel: () => void;
  }

  let { rule: initialRule, libraries, onSave, onCancel }: Props = $props();

  type ArrInstance = {
    id: number;
    name: string;
    enabled: boolean;
    base_url: string;
  };

  const defaultDefinition = (): RuleDefinition => ({
    version: 1,
    root: {
      type: "group",
      op: "and",
      children: [],
    },
  });

  const cloneDefinition = (definition: RuleDefinition | null | undefined) => {
    if (!definition) return defaultDefinition();
    try {
      return structuredClone(definition);
    } catch {
      // svelte reactive proxies are not always structured cloneable
      return JSON.parse(JSON.stringify(definition)) as RuleDefinition;
    }
  };

  const initial = (() => {
    const currentRule = initialRule;
    return {
      name: currentRule?.name ?? "",
      enabled: currentRule?.enabled ?? true,
      targetScope:
        currentRule?.target_scope ??
        (currentRule?.media_type === MediaType.Series
          ? "series"
          : "movie_version"),
      definition: cloneDefinition(currentRule?.definition),
      action: currentRule?.action,
    };
  })();

  let name = $state(initial.name);
  let enabled = $state(initial.enabled);
  let targetScope = $state<"movie_version" | "series" | "season">(
    initial.targetScope,
  );
  let definition = $state<RuleDefinition>(initial.definition);
  let tagEnabled = $state(initial.action?.tag_enabled ?? true);
  let arrTag = $state(initial.action?.arr_tag ?? "");
  let radarrServiceConfigId = $state<number | null>(
    initial.action?.radarr_service_config_id ?? null,
  );
  let sonarrServiceConfigId = $state<number | null>(
    initial.action?.sonarr_service_config_id ?? null,
  );
  let radarrInstances = $state<ArrInstance[]>([]);
  let sonarrInstances = $state<ArrInstance[]>([]);
  let saving = $state(false);
  let evaluatingLibraryChange = $state(false);
  let libraryChangeDialogOpen = $state(false);
  let pendingLibrarySelection = $state<string[] | null>(null);
  let pendingInvalidPaths = $state<string[]>([]);
  let pendingTotalPaths = $state(0);

  // preview states
  let previewDialogOpen = $state(false);
  let previewLoading = $state(false);
  let previewError = $state("");
  let previewData = $state<PaginatedResponse<RulePreviewEntry> | null>(null);
  let previewSnapshot = $state<{
    name: string | null;
    media_type: MediaType;
    target_scope: "movie_version" | "series" | "season";
    definition: RuleDefinition;
    per_page: number;
  } | null>(null);

  const PREVIEW_PER_PAGE = 25;

  const selectedMediaType = $derived(
    targetScope === "movie_version" ? MediaType.Movie : MediaType.Series,
  );

  const scopeLibraries = $derived(
    libraries.filter((library) => library.mediaType === selectedMediaType),
  );
  const scopeLibraryIds = $derived(
    new Set(scopeLibraries.map((library) => library.libraryId)),
  );
  const selectedArrInstances = $derived(
    targetScope === "movie_version" ? radarrInstances : sonarrInstances,
  );
  const selectedArrName = $derived(
    targetScope === "movie_version" ? "Radarr" : "Sonarr",
  );
  const selectedArrInstanceName = $derived.by(() => {
    const selectedId =
      targetScope === "movie_version"
        ? radarrServiceConfigId
        : sonarrServiceConfigId;
    if (selectedId === null) return null;
    return (
      selectedArrInstances.find((instance) => instance.id === selectedId)
        ?.name ?? null
    );
  });

  const normalizedTag = $derived(sanitizeTagInput(arrTag || name));

  const pathLibraryInclusionOperators = new Set<RuleConditionOperator>([
    "contains_any",
    "in",
    "equals",
  ]);

  const pathLibraryUnsupportedOperators = new Set<RuleConditionOperator>([
    "not_in",
    "not_contains_any",
    "not_equals",
    "exists",
    "not_exists",
  ]);

  // normalize library ids from a condition value, ensuring it's always an array of non empty strings
  const normalizeLibraryIds = (value: RuleCondition["value"]) =>
    (Array.isArray(value) ? value : [value])
      .filter((id): id is string | number => id !== null && id !== undefined)
      .map((id) => String(id).trim())
      .filter(Boolean);

  const normalizePathPatterns = (value: RuleCondition["value"]) =>
    (Array.isArray(value) ? value : [value])
      .filter(
        (pattern): pattern is string | number =>
          pattern !== null && pattern !== undefined,
      )
      .map((pattern) => String(pattern).trim())
      .filter(Boolean);

  const valuelessOperators = new Set<RuleConditionOperator>([
    "exists",
    "not_exists",
    "is_true",
    "is_false",
  ]);

  const isConditionValueSet = (condition: RuleCondition): boolean => {
    if (valuelessOperators.has(condition.operator)) return true;
    const value = condition.value;
    if (Array.isArray(value)) {
      return value.some(
        (item) =>
          item !== null && item !== undefined && String(item).trim() !== "",
      );
    }
    if (value === null || value === undefined) return false;
    return String(value).trim() !== "";
  };

  const hasValidConditions = (node: RuleNode): boolean => {
    if (node.type === "condition") return isConditionValueSet(node);
    if (node.children.length === 0) return false;
    return node.children.every((child) => hasValidConditions(child));
  };

  // recursively collect all library.id conditions in the rule tree
  const collectLibraryConditions = (node: RuleNode): RuleCondition[] => {
    if (node.type === "condition") {
      return node.field === "library.id" ? [node] : [];
    }
    return node.children.flatMap(collectLibraryConditions);
  };

  const collectPathConditions = (node: RuleNode): RuleCondition[] => {
    if (node.type === "condition") {
      return node.field === "media.path" ? [node] : [];
    }
    return node.children.flatMap(collectPathConditions);
  };

  const collectPathPatterns = (root: RuleDefinition["root"]): string[] => {
    const seen = new Set<string>();
    const paths: string[] = [];
    for (const condition of collectPathConditions(root)) {
      for (const pattern of normalizePathPatterns(condition.value)) {
        if (seen.has(pattern)) continue;
        seen.add(pattern);
        paths.push(pattern);
      }
    }
    return paths;
  };

  // find a single canonical library.id condition at the root level with the expected structure
  const getCanonicalLibraryCondition = (
    root: RuleDefinition["root"],
  ): RuleCondition | null => {
    const match = root.children.find(
      (child) =>
        child.type === "condition" &&
        child.field === "library.id" &&
        child.operator === "contains_any" &&
        Array.isArray(child.value),
    );
    return match?.type === "condition" ? match : null;
  };

  // read the current library scope state from the rule definition, determining selected library ids
  // and whether custom conditions are present
  const readLibraryScopeState = (
    root: RuleDefinition["root"],
  ): {
    selectedIds: string[];
    hasCustomCondition: boolean;
  } => {
    const allLibraryConditions = collectLibraryConditions(root);
    const canonical = getCanonicalLibraryCondition(root);
    const hasOnlyCanonical =
      canonical !== null &&
      allLibraryConditions.length === 1 &&
      allLibraryConditions[0] === canonical;

    return {
      selectedIds: canonical ? normalizeLibraryIds(canonical.value) : [],
      hasCustomCondition: allLibraryConditions.length > 0 && !hasOnlyCanonical,
    };
  };

  // initialize library scope state from the initial rule definition
  const initialLibraryScope = readLibraryScopeState(initial.definition.root);
  let selectedScopeLibraryIds = $state<string[]>(
    initialLibraryScope.selectedIds,
  );
  let hasCustomLibraryCondition = $state(
    initialLibraryScope.hasCustomCondition,
  );

  // recursively rebuild the rule definition, removing any library.id conditions
  const rebuildDefinitionWithoutLibraryConditions = (
    node: RuleNode,
  ): RuleNode | null => {
    if (node.type === "condition") {
      return node.field === "library.id" ? null : node;
    }

    const children = node.children
      .map(rebuildDefinitionWithoutLibraryConditions)
      .filter((child): child is RuleNode => child !== null);

    if (children.length === 0) return null;
    return {
      ...node,
      children,
    };
  };

  // apply a canonical library.id condition at the root level with the given library ids
  const applyCanonicalLibraryScope = (libraryIds: string[]) => {
    const uniqueIds = Array.from(new Set(libraryIds));

    // Remove previous canonical root library condition before re-applying.
    definition.root.children = definition.root.children.filter(
      (child) =>
        !(
          child.type === "condition" &&
          child.field === "library.id" &&
          child.operator === "contains_any"
        ),
    );

    if (uniqueIds.length > 0) {
      definition.root.children = [
        {
          type: "condition",
          field: "library.id",
          operator: "contains_any",
          value: uniqueIds,
        },
        ...definition.root.children,
      ];
    }

    definition = { ...definition };
  };

  const updateScopeLibrarySelection = (
    libraryId: string,
    selected: boolean,
  ) => {
    if (hasCustomLibraryCondition || evaluatingLibraryChange) return;
    const next = selected
      ? [...selectedScopeLibraryIds, libraryId]
      : selectedScopeLibraryIds.filter((id) => id !== libraryId);
    const filtered = next.filter((id) => scopeLibraryIds.has(id));
    void applyScopeLibrarySelectionWithValidation(
      Array.from(new Set(filtered)),
    );
  };

  const derivePathScopeLibraryIds = (
    root: RuleDefinition["root"],
  ): string[] | null => {
    const conditions = collectLibraryConditions(root);
    if (conditions.length === 0) return null;

    const ids = new Set<string>();
    for (const condition of conditions) {
      if (pathLibraryUnsupportedOperators.has(condition.operator)) return null;
      if (!pathLibraryInclusionOperators.has(condition.operator)) return null;
      const values = normalizeLibraryIds(condition.value);
      if (values.length === 0) return null;
      for (const id of values) ids.add(id);
    }

    return ids.size > 0 ? Array.from(ids) : null;
  };

  const selectedPathScopeLibraryIds = $derived.by(() => {
    const ids = derivePathScopeLibraryIds(definition.root);
    if (!ids) return null;
    const allowed = ids.filter((id) => scopeLibraryIds.has(id));
    return allowed.length > 0 ? allowed : null;
  });

  const canSaveRule = $derived(
    name.trim().length > 0 && hasValidConditions(definition.root),
  );
  const canPreviewRule = $derived(hasValidConditions(definition.root));

  const pruneInvalidPathPatternsFromNode = (
    node: RuleNode,
    invalid: Set<string>,
  ): RuleNode | null => {
    if (node.type === "condition") {
      if (node.field !== "media.path") return node;
      const patterns = normalizePathPatterns(node.value).filter(
        (pattern) => !invalid.has(pattern),
      );
      if (patterns.length === 0) return null;
      return {
        ...node,
        value: Array.isArray(node.value) ? patterns : patterns[0],
      };
    }

    const nextChildren = node.children
      .map((child) => pruneInvalidPathPatternsFromNode(child, invalid))
      .filter((child): child is RuleNode => child !== null);
    if (nextChildren.length === 0) return null;
    return {
      ...node,
      children: nextChildren,
    };
  };

  const applyPathPruning = (invalidPatterns: string[]) => {
    if (invalidPatterns.length === 0) return;
    const invalid = new Set(invalidPatterns);
    const prunedRoot = pruneInvalidPathPatternsFromNode(
      definition.root,
      invalid,
    );
    definition.root =
      prunedRoot?.type === "group"
        ? prunedRoot
        : {
            ...definition.root,
            children: [],
          };
    definition = { ...definition };
  };

  const validatePathsForScope = async (
    nextLibraryIds: string[],
  ): Promise<{ invalidPaths: string[]; totalPaths: number } | null> => {
    const paths = collectPathPatterns(definition.root);
    if (paths.length === 0) return { invalidPaths: [], totalPaths: 0 };
    try {
      const response = await post_api<{
        valid_paths: string[];
        invalid_paths: string[];
      }>("/api/rules/validate-paths", {
        media_type: selectedMediaType,
        library_ids: nextLibraryIds.length > 0 ? nextLibraryIds : null,
        paths,
      });
      return {
        invalidPaths: response.invalid_paths ?? [],
        totalPaths: paths.length,
      };
    } catch (e: any) {
      toast.error(e.message ?? "Failed to validate path criteria.");
      return null;
    }
  };

  const applyScopeLibrarySelectionWithValidation = async (
    nextLibraryIds: string[],
  ) => {
    evaluatingLibraryChange = true;
    const validation = await validatePathsForScope(nextLibraryIds);
    evaluatingLibraryChange = false;
    if (!validation) return;

    if (validation.invalidPaths.length === 0) {
      selectedScopeLibraryIds = nextLibraryIds;
      applyCanonicalLibraryScope(nextLibraryIds);
      return;
    }

    pendingLibrarySelection = nextLibraryIds;
    pendingInvalidPaths = validation.invalidPaths;
    pendingTotalPaths = validation.totalPaths;
    libraryChangeDialogOpen = true;
  };

  const confirmLibraryScopeChange = () => {
    if (!pendingLibrarySelection) return;
    selectedScopeLibraryIds = pendingLibrarySelection;
    applyCanonicalLibraryScope(pendingLibrarySelection);
    applyPathPruning(pendingInvalidPaths);
    cancelLibraryScopeChange();
  };

  const cancelLibraryScopeChange = () => {
    libraryChangeDialogOpen = false;
    pendingLibrarySelection = null;
    pendingInvalidPaths = [];
    pendingTotalPaths = 0;
  };

  const clearCustomLibraryConditions = () => {
    const cleanedRoot = rebuildDefinitionWithoutLibraryConditions(
      definition.root,
    );
    definition.root =
      cleanedRoot?.type === "group"
        ? cleanedRoot
        : {
            ...definition.root,
            children: [],
          };
    selectedScopeLibraryIds = [];
    hasCustomLibraryCondition = false;
    definition = { ...definition };
  };

  // allowed (lowercase letters, numbers, dashes, underscores)
  function sanitizeTagInput(value: string): string {
    // remove rec- if user tries to type it
    let v = value.replace(/^rec-/, "");
    // return empty if nothing left after removing disallowed chars
    if (!v) return "";
    // remove disallowed characters
    v = v.replace(/[^a-zA-Z0-9-]/g, "");
    // truncate to fit within 50 chars
    v = v.slice(0, 50);
    return `rec-${v}`;
  }

  const handleTagInput = (event: Event) => {
    const input = event.target as HTMLInputElement;
    arrTag = sanitizeTagInput(input.value);
  };

  const loadArrInstances = async () => {
    const services = await get_api<Record<string, any>>(
      "/api/settings/services",
    );
    radarrInstances = services.radarr?.instances ?? [];
    sonarrInstances = services.sonarr?.instances ?? [];
  };

  const save = async () => {
    saving = true;
    try {
      await onSave({
        name: name.trim(),
        enabled,
        media_type: selectedMediaType,
        target_scope: targetScope,
        definition,
        action: {
          candidate: true,
          tag_enabled: tagEnabled,
          arr_tag: normalizedTag,
          media_server_action: "delete",
          radarr_service_config_id:
            targetScope === "movie_version" ? radarrServiceConfigId : null,
          sonarr_service_config_id:
            targetScope === "movie_version" ? null : sonarrServiceConfigId,
        },
      });
    } finally {
      saving = false;
    }
  };

  // --- preview helpers ----
  const previewSizeLabel = (value: number | null): string =>
    value !== null ? `${value.toFixed(2)} GB` : "Unknown size";

  const fileNameFromPath = (
    path: string | null,
    fallbackFileName: string | null = null,
  ): string => {
    if (fallbackFileName && fallbackFileName.trim()) {
      return fallbackFileName.trim();
    }
    if (!path) return "Unknown file";
    const parts = path.split(/[/\\]/);
    return parts[parts.length - 1] || "Unknown file";
  };

  const previewBadges = (entry: RulePreviewEntry): string[] => {
    const badges: string[] = [];
    if (entry.season_id !== null) {
      if (entry.season_max_video_height) {
        badges.push(`${entry.season_max_video_height}p`);
      }
      if (entry.season_has_dolby_vision) badges.push("DV");
      else if (entry.season_has_hdr) badges.push("HDR");
    } else {
      if (entry.version_video_resolution) {
        badges.push(entry.version_video_resolution);
      } else if (entry.version_video_height) {
        badges.push(`${entry.version_video_height}p`);
      }
      if (entry.version_video_dolby_vision) badges.push("DV");
      else if (entry.version_video_hdr) badges.push("HDR");
      if (entry.version_video_codec_family) {
        badges.push(entry.version_video_codec_family.toUpperCase());
      }
    }
    badges.push(previewSizeLabel(entry.estimated_space_gb));
    return badges;
  };

  const previewRuleSummary = (entry: RulePreviewEntry): string[] =>
    entry.reason_tokens.slice(0, 2);

  const previewExtraRuleCount = (entry: RulePreviewEntry): number =>
    Math.max(0, entry.reason_tokens.length - 2);

  const buildPreviewSnapshot = () => ({
    name: name.trim() || null,
    media_type: selectedMediaType,
    target_scope: targetScope,
    definition: cloneDefinition(definition),
    per_page: PREVIEW_PER_PAGE,
  });

  const loadPreviewPage = async (page: number, openDialog = true) => {
    const snapshot = page === 1 ? buildPreviewSnapshot() : previewSnapshot;
    if (!snapshot) return;
    previewLoading = true;
    previewError = "";
    if (page === 1) previewData = null;
    if (openDialog) previewDialogOpen = true;
    try {
      previewData = await post_api<PaginatedResponse<RulePreviewEntry>>(
        "/api/rules/preview",
        {
          ...snapshot,
          page,
        },
      );
      previewSnapshot = snapshot;
    } catch (e: any) {
      previewData = null;
      previewError = e.message ?? "Failed to preview rule matches.";
    } finally {
      previewLoading = false;
    }
  };
  // --- preview helpers ----

  // synchronize library scope state with rule definition, ensuring the canonical library
  // condition is the single source of truth
  $effect(() => {
    const state = readLibraryScopeState(definition.root);
    const filteredIds = state.selectedIds.filter((id) =>
      scopeLibraryIds.has(id),
    );
    selectedScopeLibraryIds = filteredIds;
    hasCustomLibraryCondition = state.hasCustomCondition;

    if (
      !state.hasCustomCondition &&
      filteredIds.length !== state.selectedIds.length
    ) {
      applyCanonicalLibraryScope(filteredIds);
    }
  });

  $effect(() => {
    if (hasCustomLibraryCondition) return;
    const filtered = selectedScopeLibraryIds.filter((id) =>
      scopeLibraryIds.has(id),
    );
    if (filtered.length !== selectedScopeLibraryIds.length) {
      selectedScopeLibraryIds = filtered;
      applyCanonicalLibraryScope(filtered);
    }
  });

  onMount(() => {
    loadArrInstances();
  });
</script>

<div class="space-y-6">
  <div class="flex flex-col md:flex-row items-center justify-between gap-3">
    <div class="flex items-center gap-3">
      <Button
        class="cursor-pointer"
        variant="ghost"
        size="icon"
        onclick={onCancel}
      >
        <ArrowLeft class="size-5 text-primary" />
      </Button>
      <div>
        <h2 class="text-xl font-semibold text-foreground">
          {initialRule ? "Edit Rule" : "New Rule"}
        </h2>
        <p class="text-sm text-muted-foreground">
          Build nested AND/OR rules for cleanup candidates.
        </p>
      </div>
    </div>
    <div class="flex items-center gap-2">
      <Button
        type="button"
        variant="secondary"
        onclick={() => void loadPreviewPage(1)}
        disabled={previewLoading || !canPreviewRule}
        class="cursor-pointer"
      >
        {#if previewLoading}
          <Eye class="size-4 animate-spin" /> Previewing...
        {:else}
          <Eye class="size-4" /> Preview Matches
        {/if}
      </Button>
      <Button
        onclick={save}
        disabled={saving || !canSaveRule}
        class="gap-2 cursor-pointer"
      >
        <Save class="size-4" />
        {saving ? "Saving..." : "Save Rule"}
      </Button>
    </div>
  </div>

  <div class="flex flex-col gap-4 rounded-lg border border-border bg-card p-5">
    <!-- toggle -->
    <div class="flex items-end justify-start gap-3">
      <span class="text-sm font-medium text-foreground">Enabled</span>
      <Switch
        checked={enabled}
        onCheckedChange={(value) => (enabled = value)}
      />
    </div>

    <div class="flex flex-col md:flex-row space-x-2 space-y-2">
      <!-- name -->
      <div class="space-y-2 w-full">
        <Label for="rule-name" class="block text-sm font-medium text-foreground"
          >Name</Label
        >
        <Input
          id="rule-name"
          class="input-hover-el text-foreground"
          bind:value={name}
          placeholder="Rule name"
        />
      </div>

      <!-- target -->
      <div class="space-y-2 w-full">
        <Label class="text-sm font-medium text-foreground">Target</Label>
        <Select.Root type="single" bind:value={targetScope}>
          <Select.Trigger
            class="w-full flex-10 bg-card text-card-foreground cursor-pointer"
          >
            {targetScope === "movie_version"
              ? "Movie version"
              : targetScope === "series"
                ? "Series"
                : "Season"}
          </Select.Trigger>
          <Select.Content>
            <Select.Item value="movie_version" label="Movie version">
              Movie version
            </Select.Item>
            <Select.Item value="series" label="Series">Series</Select.Item>
            <Select.Item value="season" label="Season">Season</Select.Item>
          </Select.Content>
        </Select.Root>
      </div>
    </div>
  </div>

  <div class="rounded-lg border border-border bg-card p-5 space-y-4">
    <div>
      <h3 class="font-semibold text-foreground">Library Scope</h3>
      <p class="text-sm text-muted-foreground">
        Select libraries this rule should target. Leave all unselected to apply
        the rule to every library in this target.
      </p>
    </div>

    {#if hasCustomLibraryCondition}
      <Notice type="warning" title="Custom Library Condition Detected">
        Library conditions are currently customized in the rule tree. Use
        <strong>Clear Custom Library Conditions</strong> to return to the dedicated
        scope selector.
      </Notice>
      <Button
        type="button"
        size="sm"
        variant="secondary"
        class="cursor-pointer"
        onclick={clearCustomLibraryConditions}
      >
        Clear Custom Library Conditions
      </Button>
    {/if}

    {#if scopeLibraries.length > 0}
      <div class="space-y-2">
        {#each scopeLibraries as library}
          <div class="flex items-center gap-2">
            <Switch
              id={`scope-library-${library.libraryId}`}
              checked={selectedScopeLibraryIds.includes(library.libraryId)}
              disabled={hasCustomLibraryCondition || evaluatingLibraryChange}
              onCheckedChange={(checked) =>
                void updateScopeLibrarySelection(library.libraryId, checked)}
            />
            <div class="flex items-center gap-1.5">
              <div class="w-4 h-4 shrink-0">
                {#if library.serviceType === SettingsTab.Jellyfin}
                  <JellyfinSVG />
                {:else if library.serviceType === SettingsTab.Plex}
                  <PlexSVG />
                {:else if library.serviceType === SettingsTab.Emby}
                  <EmbySVG />
                {/if}
              </div>
              <Label
                class="text-foreground"
                for={`scope-library-${library.libraryId}`}
              >
                {library.libraryName}
              </Label>
            </div>
          </div>
        {/each}
      </div>
    {:else}
      <p class="text-sm text-muted-foreground">
        No matching libraries are configured.
      </p>
    {/if}
  </div>

  <div class="rounded-lg border border-border bg-card p-6 space-y-5">
    <div>
      <h3 class="font-semibold text-foreground">Conditions</h3>
      <p class="text-sm text-muted-foreground">
        Groups can be nested to combine broad filters with precise media-info
        checks.
      </p>
      <p class="text-xs text-muted-foreground mt-1">
        Up to 5 groups total are supported per rule.
      </p>
      {#if !hasValidConditions(definition.root)}
        <p class="text-xs text-amber-500 mt-1">
          Add at least one complete condition before saving.
        </p>
      {/if}
    </div>
    <div class="rounded-lg border border-border/60 bg-muted/20 p-4 md:p-5">
      <RuleNodeEditor
        node={definition.root}
        pathPickerMediaType={selectedMediaType}
        pathPickerLibraryIds={selectedPathScopeLibraryIds}
        onChange={() => (definition = { ...definition })}
      />
    </div>
  </div>

  <div class="rounded-lg border border-border bg-card p-5">
    <div class="flex items-center justify-between gap-3">
      <div class="w-full">
        <div class="flex justify-between w-full">
          <h3 class="font-semibold text-foreground flex items-center gap-2">
            {#if selectedArrName === "Radarr"}
              <RadarrSVG class="size-4 inline" /> {selectedArrName}
            {:else if selectedArrName === "Sonarr"}
              <SonarrSVG class="size-4 inline" /> {selectedArrName}
            {/if}
          </h3>
          <div class="flex items-center gap-2">
            <span class="text-sm font-medium text-foreground">Enabled</span>
            <Switch
              checked={tagEnabled}
              onCheckedChange={(value) => (tagEnabled = value)}
            />
          </div>
        </div>
        <p class="mt-1 mb-3 text-sm text-muted-foreground">
          Matching items become cleanup candidates and can be tagged in {selectedArrName}.
        </p>
      </div>
    </div>

    {#if tagEnabled}
      <div class="flex flex-col md:flex-row space-x-2 space-y-2">
        <div class="space-y-2 w-full">
          <Label class="space-y-2">
            <span class="text-sm font-medium text-foreground">
              {`${selectedArrName} Instance`}
            </span>
          </Label>
          <Select.Root
            type="single"
            value={targetScope === "movie_version"
              ? radarrServiceConfigId !== null
                ? String(radarrServiceConfigId)
                : undefined
              : sonarrServiceConfigId !== null
                ? String(sonarrServiceConfigId)
                : undefined}
            onValueChange={(value) => {
              const nextValue = value ? Number(value) : null;
              if (targetScope === "movie_version") {
                radarrServiceConfigId = nextValue;
              } else {
                sonarrServiceConfigId = nextValue;
              }
            }}
          >
            <Select.Trigger
              class="w-full flex-10 bg-card text-card-foreground cursor-pointer"
            >
              {#if selectedArrInstanceName}
                {selectedArrInstanceName}
              {:else}
                No instance selected
              {/if}
            </Select.Trigger>
            <Select.Content>
              <Select.Item value="__none" disabled label="No instance selected">
                No instance selected
              </Select.Item>
              {#each selectedArrInstances as instance}
                <Select.Item
                  value={String(instance.id)}
                  label={`${instance.name}${instance.enabled ? "" : " (disabled)"}`}
                >
                  {instance.name}{instance.enabled ? "" : " (disabled)"}
                </Select.Item>
              {/each}
            </Select.Content>
          </Select.Root>
        </div>

        <div class="space-y-2 w-full">
          <Label class="text-sm font-medium text-foreground">Managed Tag</Label>
          <Input
            class="input-hover-el text-foreground"
            bind:value={arrTag}
            placeholder="rec-custom-tag"
            max={25}
            oninput={handleTagInput}
          />
          <p class="text-xs text-muted-foreground">
            Will be saved as {normalizedTag}
          </p>
        </div>
      </div>
    {/if}
  </div>
</div>

<!-- preview dialog -->
<Dialog.Root bind:open={previewDialogOpen}>
  <Dialog.Content
    onInteractOutside={(e) => {
      e.preventDefault(); // prevent clicking out of preview without hitting X
    }}
    class="w-full sm:max-w-4xl overflow-hidden bg-card text-card-foreground"
  >
    <Dialog.Header>
      <Dialog.Title>Preview Matches</Dialog.Title>
      <Dialog.Description>
        Dry run only. Previewing does not save the rule or create cleanup
        candidates.
      </Dialog.Description>
    </Dialog.Header>

    <div class="space-y-4">
      <div class="flex items-center justify-between gap-3">
        <div class="text-sm text-muted-foreground">
          {#if previewData}
            <strong>{previewData.total}</strong> matching item{previewData.total ===
            1
              ? ""
              : "s"}
          {:else}
            No preview loaded.
          {/if}
        </div>
        {#if previewData && previewData.total_pages > 1}
          <CompactPagination
            currentPage={previewData.page}
            totalPages={previewData.total_pages}
            onPageChange={(page) => void loadPreviewPage(page, false)}
          />
        {/if}
      </div>
      <div class="h-[55vh] overflow-y-auto overflow-x-hidden pr-2">
        {#if previewError}
          <Notice type="error" title="Preview Failed">
            {previewError}
          </Notice>
        {:else if previewLoading}
          <div class="flex justify-center items-center py-20">
            <Spinner class="size-12 text-primary" />
          </div>
        {:else if previewData && previewData.items.length === 0}
          <div
            class="rounded-md border border-border bg-muted/20 p-6 text-sm text-muted-foreground"
          >
            No matching items for the current preview.
          </div>
        {:else if previewData}
          <div class="space-y-3 pr-1">
            {#each previewData.items as entry, index (`${entry.media_type}-${entry.media_id}-${entry.movie_version_id ?? "base"}-${entry.season_id ?? "none"}-${index}`)}
              {@const previewRules = previewRuleSummary(entry)}
              {@const extraRuleCount = previewExtraRuleCount(entry)}
              <div class="rounded-lg border border-border bg-muted/20 p-4">
                <div class="flex gap-3">
                  <PosterThumb
                    mediaType={entry.media_type}
                    posterUrl={entry.poster_url}
                  />
                  <div class="min-w-0 flex-1 space-y-2">
                    <div
                      class="flex flex-wrap items-start justify-between gap-2"
                    >
                      <div class="min-w-0">
                        <div class="text-sm font-medium text-foreground">
                          <span class="break-all">{entry.media_title}</span>
                          {#if entry.media_year}
                            <span class="text-muted-foreground"
                              >({entry.media_year})</span
                            >
                          {/if}
                        </div>
                        <div class="mt-1 flex flex-wrap items-center gap-2">
                          <MediaTypeBadge mediaType={entry.media_type} />
                          {#if entry.season_id !== null}
                            <span class="text-xs text-muted-foreground">
                              Season {entry.season_number ?? "?"}
                            </span>
                          {:else if entry.movie_version_id !== null}
                            <span
                              class="text-xs text-muted-foreground break-all"
                            >
                              {fileNameFromPath(
                                entry.version_path,
                                entry.version_file_name,
                              )}
                            </span>
                          {/if}
                        </div>
                      </div>
                    </div>

                    <div class="flex flex-wrap gap-1.5">
                      {#each previewBadges(entry) as badge}
                        <span
                          class="text-xs leading-5 px-2 rounded-2xl border border-border bg-card text-foreground"
                        >
                          {badge}
                        </span>
                      {/each}
                    </div>

                    <div class="flex flex-wrap gap-1.5">
                      {#each previewRules as rule}
                        <span
                          class="text-xs leading-5 px-2 rounded-2xl border border-border bg-card text-foreground"
                        >
                          {rule}
                        </span>
                      {/each}
                      {#if extraRuleCount > 0}
                        <span
                          class="text-xs leading-5 px-2 rounded-full border border-border bg-card text-muted-foreground"
                        >
                          +{extraRuleCount} more
                        </span>
                      {/if}
                    </div>
                  </div>
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    </div>
  </Dialog.Content>
</Dialog.Root>

<!-- library change confirmation dialog -->
<AlertDialog.Root
  open={libraryChangeDialogOpen}
  onOpenChange={(open) => {
    if (!open) cancelLibraryScopeChange();
  }}
>
  <AlertDialog.Content
    class="bg-card border border-border rounded-lg p-6 max-w-xl w-full text-foreground"
  >
    <AlertDialog.Header>
      <AlertDialog.Title
        class="text-xl font-semibold text-foreground mb-2 flex items-center gap-2"
      >
        <TriangleAlert class="size-5 text-amber-500" />
        Path Criteria Needs Confirmation
      </AlertDialog.Title>
      <AlertDialog.Description class="text-muted-foreground space-y-3">
        {#if pendingInvalidPaths.length >= pendingTotalPaths && pendingTotalPaths > 0}
          <p>
            This library scope change invalidates all current path criteria.
            Confirming will clear all path conditions from this rule.
          </p>
        {:else}
          <p>
            This library scope change invalidates some path criteria. Confirming
            will remove only the invalid patterns and keep the rest.
          </p>
        {/if}
        {#if pendingInvalidPaths.length > 0}
          <div
            class="rounded-md border border-border bg-muted/30 p-3 max-h-44 overflow-y-auto"
          >
            <p
              class="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2"
            >
              Invalid Patterns ({pendingInvalidPaths.length})
            </p>
            <ul class="space-y-1.5">
              {#each pendingInvalidPaths as pattern}
                <li class="font-mono text-xs break-all text-foreground/90">
                  {pattern}
                </li>
              {/each}
            </ul>
          </div>
        {/if}
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer class="flex justify-end gap-3 pt-4">
      <AlertDialog.Cancel
        class="cursor-pointer hover text-foreground bg-secondary"
        onclick={cancelLibraryScopeChange}
      >
        Cancel
      </AlertDialog.Cancel>
      <AlertDialog.Action
        class="cursor-pointer hover"
        onclick={confirmLibraryScopeChange}
      >
        {#if pendingInvalidPaths.length >= pendingTotalPaths && pendingTotalPaths > 0}
          Clear Paths and Continue
        {:else}
          Remove Invalid Paths and Continue
        {/if}
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
