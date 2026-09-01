<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import { Checkbox } from "$lib/components/ui/checkbox/index.js";
  import { get_api, post_api } from "$lib/api";
  import { onMount } from "svelte";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { Textarea } from "$lib/components/ui/textarea/index.js";
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
  import { isConditionValueSet } from "$lib/components/settings/rules/rule-condition-value.js";
  import { movieRequesterWatchSummary } from "$lib/components/settings/rules/requester-watch-summary.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { toast } from "svelte-sonner";
  import {
    MEDIA_SERVERS,
    MediaType,
    SettingsTab,
    type LibraryType,
    type PaginatedRulePreviewResponse,
    type ReclaimRule,
    type RuleCondition,
    type RuleConditionOperator,
    type RuleDefinition,
    type RuleNode,
    type RequesterWatchExplain,
    type RulePreviewEntry,
  } from "$lib/types/shared";
  import { formatFileSize } from "$lib/utils/formatters";

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
      description: currentRule?.description ?? "",
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
  let description = $state(initial.description);
  let enabled = $state(initial.enabled);
  let targetScope = $state<"movie_version" | "series" | "season" | "episode">(
    initial.targetScope,
  );
  let definition = $state<RuleDefinition>(initial.definition);
  let outcome = $state<"candidate" | "protect">(
    initial.action?.outcome === "protect" ? "protect" : "candidate",
  );
  let tagEnabled = $state(initial.action?.tag_enabled ?? false);
  let autoDeleteEnabled = $state(initial.action?.auto_delete_enabled ?? false);
  let moveInsteadOfDelete = $state(
    initial.action?.move_instead_of_delete ?? false,
  );
  let arrTag = $state(initial.action?.arr_tag ?? "");
  let autoDeleteDelayDays = $state<string | number>(
    initial.action?.auto_delete_delay_days?.toString() ?? "",
  );

  let radarrArrAction = $state<"delete" | "unmonitor" | "unmonitor_only">(
    initial.targetScope === "movie_version"
      ? (initial.action?.arr_action ?? "delete")
      : "delete",
  );
  let sonarrArrAction = $state<"delete" | "unmonitor" | "unmonitor_only">(
    initial.targetScope !== "movie_version"
      ? (initial.action?.arr_action ?? "delete")
      : "delete",
  );
  const arrAction = $derived(
    targetScope === "movie_version" ? radarrArrAction : sonarrArrAction,
  );
  let radarrServiceConfigIds = $state<number[]>(
    initial.action?.radarr_service_config_ids ??
      (initial.action?.radarr_service_config_id != null
        ? [initial.action.radarr_service_config_id]
        : []),
  );
  let sonarrServiceConfigIds = $state<number[]>(
    initial.action?.sonarr_service_config_ids ??
      (initial.action?.sonarr_service_config_id != null
        ? [initial.action.sonarr_service_config_id]
        : []),
  );
  let mediaServerCount = $state(0);
  let radarrInstances = $state<ArrInstance[]>([]);
  let sonarrInstances = $state<ArrInstance[]>([]);
  let saving = $state(false);
  let evaluatingLibraryChange = $state(false);
  let libraryChangeDialogOpen = $state(false);
  let pendingLibrarySelection = $state<string[] | null>(null);
  let pendingInvalidPaths = $state<PathValidationCriterion[]>([]);
  let pendingTotalPaths = $state(0);

  // preview states
  let previewDialogOpen = $state(false);
  let explainDialogOpen = $state(false);
  let explainLoading = $state(false);
  let explainError = $state<string | null>(null);
  let explainData = $state<RequesterWatchExplain | null>(null);
  let previewLoading = $state(false);
  let previewError = $state("");
  let previewData = $state<PaginatedRulePreviewResponse | null>(null);
  let previewSnapshot = $state<{
    name: string | null;
    media_type: MediaType;
    target_scope: "movie_version" | "series" | "season" | "episode";
    definition: RuleDefinition;
    outcome: "candidate" | "protect";
    per_page: number;
  } | null>(null);

  const PREVIEW_PER_PAGE = 25;

  const selectedMediaType = $derived(
    targetScope === "movie_version" ? MediaType.Movie : MediaType.Series,
  );

  const scopeLibraries = $derived(
    libraries.filter((library) => library.mediaType === selectedMediaType),
  );

  // One media server needs no disambiguation; several do. Libraries only ever
  // come from the main server, so this names that server rather than telling
  // several apart -- but with a second Plex configured, "Movies" alone does
  // not say whose Movies the rule is scoped to.
  const qualifyLibraries = $derived(mediaServerCount > 1);

  const libraryInstanceLabel = (library: LibraryType) =>
    library.serviceName ||
    (library.serviceConfigId !== null
      ? `${library.serviceType} #${library.serviceConfigId}`
      : library.serviceType);

  const libraryGroups = $derived.by(() => {
    const byInstance = new Map<
      string,
      { key: string; label: string; libraries: LibraryType[] }
    >();
    for (const library of scopeLibraries) {
      const key = `${library.serviceType}:${library.serviceConfigId ?? "?"}`;
      const existing = byInstance.get(key);
      if (existing) {
        existing.libraries.push(library);
        continue;
      }
      byInstance.set(key, {
        key,
        label: libraryInstanceLabel(library),
        libraries: [library],
      });
    }
    return [...byInstance.values()].sort((l, r) =>
      l.label.localeCompare(r.label),
    );
  });
  const scopeLibraryIds = $derived(
    new Set(scopeLibraries.map((library) => library.libraryId)),
  );
  const selectedArrInstances = $derived(
    targetScope === "movie_version" ? radarrInstances : sonarrInstances,
  );
  const selectedArrName = $derived(
    targetScope === "movie_version" ? "Radarr" : "Sonarr",
  );
  const selectedArrConfigIds = $derived(
    targetScope === "movie_version"
      ? radarrServiceConfigIds
      : sonarrServiceConfigIds,
  );

  const toggleArrInstance = (instanceId: number, checked: boolean) => {
    const current =
      targetScope === "movie_version"
        ? radarrServiceConfigIds
        : sonarrServiceConfigIds;
    const next = checked
      ? Array.from(new Set([...current, instanceId]))
      : current.filter((id) => id !== instanceId);
    if (targetScope === "movie_version") {
      radarrServiceConfigIds = next;
    } else {
      sonarrServiceConfigIds = next;
    }
  };

  const normalizedTag = $derived(sanitizeTagInput(arrTag || name));

  const pathLibraryInclusionOperators = new Set<RuleConditionOperator>([
    "contains_any",
    "contains_all",
    "in",
    "equals",
  ]);

  const pathLibraryUnsupportedOperators = new Set<RuleConditionOperator>([
    "not_in",
    "not_contains_any",
    "not_contains_all",
    "not_equals",
    "exists",
    "not_exists",
  ]);

  const pathValidationOperators = new Set<RuleConditionOperator>([
    "equals",
    "in",
    "contains_any",
    "contains_all",
    "matches_any_regex",
  ]);

  type PathValidationField = "media.path" | "media.file_name";
  type PathValidationCriterion = {
    field: PathValidationField;
    operator: RuleConditionOperator;
    value: string;
  };

  const isPathValidationField = (field: string): field is PathValidationField =>
    field === "media.path" || field === "media.file_name";

  const pathCriterionKey = (
    field: PathValidationField,
    operator: RuleConditionOperator,
    value: string,
  ) => `${field}::${operator}::${value}`;

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
      return isPathValidationField(node.field) ? [node] : [];
    }
    return node.children.flatMap(collectPathConditions);
  };

  const collectPathCriteria = (
    root: RuleDefinition["root"],
  ): PathValidationCriterion[] => {
    const seen = new Set<string>();
    const criteria: PathValidationCriterion[] = [];
    for (const condition of collectPathConditions(root)) {
      if (!isPathValidationField(condition.field)) continue;
      if (!pathValidationOperators.has(condition.operator)) continue;
      for (const value of normalizePathPatterns(condition.value)) {
        const key = pathCriterionKey(
          condition.field,
          condition.operator,
          value,
        );
        if (seen.has(key)) continue;
        seen.add(key);
        criteria.push({
          field: condition.field,
          operator: condition.operator,
          value,
        });
      }
    }
    return criteria;
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

  const pruneInvalidPathCriteriaFromNode = (
    node: RuleNode,
    invalid: Set<string>,
  ): RuleNode | null => {
    if (node.type === "condition") {
      if (!isPathValidationField(node.field)) return node;
      if (!pathValidationOperators.has(node.operator)) return node;
      const field = node.field;
      const values = normalizePathPatterns(node.value).filter(
        (value) => !invalid.has(pathCriterionKey(field, node.operator, value)),
      );
      if (values.length === 0) return null;
      return {
        ...node,
        value: Array.isArray(node.value) ? values : values[0],
      };
    }

    const nextChildren = node.children
      .map((child) => pruneInvalidPathCriteriaFromNode(child, invalid))
      .filter((child): child is RuleNode => child !== null);
    if (nextChildren.length === 0) return null;
    return {
      ...node,
      children: nextChildren,
    };
  };

  const applyPathPruning = (invalidCriteria: PathValidationCriterion[]) => {
    if (invalidCriteria.length === 0) return;
    const invalid = new Set(
      invalidCriteria.map((criterion) =>
        pathCriterionKey(criterion.field, criterion.operator, criterion.value),
      ),
    );
    const prunedRoot = pruneInvalidPathCriteriaFromNode(
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
  ): Promise<{
    invalidCriteria: PathValidationCriterion[];
    totalCriteria: number;
  } | null> => {
    const criteria = collectPathCriteria(definition.root);
    if (criteria.length === 0) return { invalidCriteria: [], totalCriteria: 0 };
    try {
      const response = await post_api<{
        valid_paths: string[];
        invalid_paths: string[];
        valid_conditions?: PathValidationCriterion[];
        invalid_conditions?: PathValidationCriterion[];
      }>("/api/rules/validate-paths", {
        media_type: selectedMediaType,
        library_ids: nextLibraryIds.length > 0 ? nextLibraryIds : null,
        conditions: criteria,
      });
      const invalidCriteria =
        response.invalid_conditions && response.invalid_conditions.length > 0
          ? response.invalid_conditions
          : (response.invalid_paths ?? []).map((value) => ({
              field: "media.path" as const,
              operator: "matches_any_regex" as const,
              value,
            }));
      return {
        invalidCriteria,
        totalCriteria: criteria.length,
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

    if (validation.invalidCriteria.length === 0) {
      selectedScopeLibraryIds = nextLibraryIds;
      applyCanonicalLibraryScope(nextLibraryIds);
      return;
    }

    pendingLibrarySelection = nextLibraryIds;
    pendingInvalidPaths = validation.invalidCriteria;
    pendingTotalPaths = validation.totalCriteria;
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
    // Counted across every media server type, not just the one that is main:
    // it is what decides whether a bare library name is ambiguous.
    mediaServerCount = MEDIA_SERVERS.reduce(
      (total, serverKey) =>
        total + (services[serverKey]?.instances?.length ?? 0),
      0,
    );
  };

  const save = async () => {
    saving = true;
    try {
      const rawAutoDeleteDelay = String(autoDeleteDelayDays).trim();
      const autoDeleteDelay = rawAutoDeleteDelay
        ? Number(rawAutoDeleteDelay)
        : null;
      if (
        autoDeleteEnabled &&
        autoDeleteDelay !== null &&
        (!Number.isInteger(autoDeleteDelay) ||
          autoDeleteDelay < 0 ||
          autoDeleteDelay > 3650)
      ) {
        toast.error("Auto-delete delay must be a whole number from 0 to 3650");
        return;
      }
      await onSave({
        name: name.trim(),
        description: description.trim() || null,
        enabled,
        media_type: selectedMediaType,
        target_scope: targetScope,
        definition,
        action: {
          outcome,
          candidate: outcome === "candidate",
          tag_enabled: outcome === "candidate" ? tagEnabled : false,
          arr_tag: outcome === "candidate" ? normalizedTag : null,
          arr_action: arrAction,
          media_server_action: outcome === "candidate" ? "delete" : null,
          auto_delete_enabled:
            outcome === "candidate" ? autoDeleteEnabled : false,
          auto_delete_delay_days:
            outcome === "candidate" && autoDeleteEnabled
              ? autoDeleteDelay
              : null,
          move_instead_of_delete:
            outcome === "candidate" ? moveInsteadOfDelete : false,
          radarr_service_config_id:
            outcome === "candidate" &&
            targetScope === "movie_version" &&
            radarrServiceConfigIds.length === 1
              ? radarrServiceConfigIds[0]
              : null,
          sonarr_service_config_id:
            outcome === "candidate" &&
            targetScope !== "movie_version" &&
            sonarrServiceConfigIds.length === 1
              ? sonarrServiceConfigIds[0]
              : null,
          radarr_service_config_ids:
            outcome === "candidate" && targetScope === "movie_version"
              ? radarrServiceConfigIds
              : [],
          sonarr_service_config_ids:
            outcome === "candidate" && targetScope !== "movie_version"
              ? sonarrServiceConfigIds
              : [],
        },
      });
    } finally {
      saving = false;
    }
  };

  // --- preview helpers ----
  const previewSizeLabel = (value: number | null): string =>
    formatFileSize(value);

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
    badges.push(previewSizeLabel(entry.estimated_space_bytes));
    return badges;
  };

  const previewRuleSummary = (entry: RulePreviewEntry): string[] =>
    entry.reason_tokens.slice(0, 2);

  const previewExtraRuleCount = (entry: RulePreviewEntry): number =>
    Math.max(0, entry.reason_tokens.length - 2);

  // Requester watch state has too many moving parts to debug from a boolean,
  // so each matching row can ask the backend to show its working. Both fields
  // are answered by the same explanation.
  const REQUESTER_HAS_WATCHED_FIELD = "seerr.requester_has_watched";
  const REQUESTER_WATCHED_AFTER_REQUEST_FIELD =
    "seerr.requester_watched_after_request";
  const REQUESTER_WATCH_FIELDS = [
    REQUESTER_HAS_WATCHED_FIELD,
    REQUESTER_WATCHED_AFTER_REQUEST_FIELD,
  ];

  const ruleUsesRequesterWatch = $derived.by(() => {
    const serialized = JSON.stringify(definition.root);
    return REQUESTER_WATCH_FIELDS.some((field) => serialized.includes(field));
  });

  // The explanation answers both halves whichever one the rule asked for, so
  // track which half is in play and stop the other from reading like a verdict
  // this rule acted on. Read from the previewed rule, since the explanation
  // describes that match rather than any edit made since.
  const requesterWatchFieldsUsed = $derived.by(() => {
    const serialized = JSON.stringify(
      (previewSnapshot?.definition ?? definition).root,
    );
    const hasWatched = serialized.includes(REQUESTER_HAS_WATCHED_FIELD);
    const afterRequest = serialized.includes(
      REQUESTER_WATCHED_AFTER_REQUEST_FIELD,
    );
    // Finding neither means we cannot tell which half matters, so leave both
    // reading normally rather than greying out the answer being looked for.
    if (!hasWatched && !afterRequest) {
      return { hasWatched: true, afterRequest: true };
    }
    return { hasWatched, afterRequest };
  });

  // Watching something before requesting it only counts against the item when
  // the rule asks the gated question and settings still enforce the gate.
  const requestDateGateInPlay = $derived(
    requesterWatchFieldsUsed.afterRequest &&
      explainData !== null &&
      !explainData.request_date_gate_ignored,
  );

  const explainMoment = (value: string | null): string => {
    if (!value) return "unknown";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  };

  const explainScopeFor = (entry: RulePreviewEntry): string => {
    if (entry.episode_id !== null) return "episode";
    if (entry.season_id !== null) return "season";
    if (entry.media_type === "series") return "series";
    return "movie_version";
  };

  const openRequesterWatchExplain = async (entry: RulePreviewEntry) => {
    if (entry.tmdb_id === null) return;
    explainDialogOpen = true;
    explainLoading = true;
    explainError = null;
    explainData = null;
    const params = new URLSearchParams({
      media_type: entry.media_type === "series" ? "series" : "movie",
      tmdb_id: String(entry.tmdb_id),
      target_scope: explainScopeFor(entry),
    });
    if (entry.season_number !== null && entry.season_number !== undefined) {
      params.set("season_number", String(entry.season_number));
    }
    if (entry.episode_number !== null && entry.episode_number !== undefined) {
      params.set("episode_number", String(entry.episode_number));
    }
    try {
      explainData = await get_api<RequesterWatchExplain>(
        `/api/rules/requester-watch-explain?${params.toString()}`,
      );
    } catch (error) {
      explainError =
        error instanceof Error ? error.message : "Failed to load explanation";
    } finally {
      explainLoading = false;
    }
  };

  const buildPreviewSnapshot = () => ({
    name: name.trim() || null,
    media_type: selectedMediaType,
    target_scope: targetScope,
    definition: cloneDefinition(definition),
    outcome,
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
      previewData = await post_api<PaginatedRulePreviewResponse>(
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
          Build nested AND/OR rules for cleanup candidates or automated
          protections.
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
        <Select.Root
          type="single"
          value={targetScope}
          onValueChange={(value) => {
            if (
              value === "movie_version" ||
              value === "series" ||
              value === "season" ||
              value === "episode"
            ) {
              targetScope = value;
            }
          }}
        >
          <Select.Trigger
            class="w-full flex-10 bg-card text-card-foreground cursor-pointer"
          >
            {targetScope === "movie_version"
              ? "Movie version"
              : targetScope === "series"
                ? "Series"
                : targetScope === "season"
                  ? "Season"
                  : "Episode"}
          </Select.Trigger>
          <Select.Content>
            <Select.Item value="movie_version" label="Movie version">
              Movie version
            </Select.Item>
            <Select.Item value="series" label="Series">Series</Select.Item>
            <Select.Item value="season" label="Season">Season</Select.Item>
            <Select.Item value="episode" label="Episode">Episode</Select.Item>
          </Select.Content>
        </Select.Root>
      </div>
    </div>

    <div class="space-y-2">
      <Label
        for="rule-description"
        class="block text-sm font-medium text-foreground"
        >Description <span class="font-normal text-muted-foreground"
          >(optional)</span
        ></Label
      >
      <Textarea
        id="rule-description"
        class="input-hover-el text-foreground"
        bind:value={description}
        placeholder="Describe what this rule is for"
      />
    </div>

    <div class="space-y-2">
      <Label class="text-sm font-medium text-foreground">Outcome</Label>
      <Select.Root
        type="single"
        value={outcome}
        onValueChange={(value) => {
          if (value === "candidate" || value === "protect") outcome = value;
        }}
      >
        <Select.Trigger
          class="w-full bg-card text-card-foreground cursor-pointer"
        >
          {outcome === "protect"
            ? "Create automated protection"
            : "Create cleanup candidate"}
        </Select.Trigger>
        <Select.Content>
          <Select.Item value="candidate" label="Create cleanup candidate">
            Create cleanup candidate
          </Select.Item>
          <Select.Item value="protect" label="Create automated protection">
            Create automated protection
          </Select.Item>
        </Select.Content>
      </Select.Root>
      <p class="text-xs text-muted-foreground">
        {outcome === "protect"
          ? "Matching items are protected on each cleanup scan. Protection always takes precedence over candidate rules."
          : "Matching items become cleanup candidates and can use the configured Arr action."}
      </p>

      {#if outcome === "protect"}
        <Notice type="info" title="Rule-Managed Protection">
          Matching items in the selected library scope are protected
          automatically during cleanup scans. These protections are read-only on
          the Protected page and are removed when the rule no longer matches.
        </Notice>
      {/if}
    </div>
  </div>

  <div class="rounded-lg border border-border bg-card p-5 space-y-4">
    <div>
      <h3 class="font-semibold text-foreground">Library Scope</h3>
      <p class="text-sm text-muted-foreground">
        Select libraries this rule should target. Leave all unselected to apply
        the rule to every library in this target.
      </p>
      {#if qualifyLibraries}
        <p class="text-xs text-muted-foreground mt-1">
          Libraries come from the main media server. Linked servers contribute
          watch data, not library contents.
        </p>
      {/if}
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
      <div class="space-y-4">
        {#each libraryGroups as group (group.key)}
          <div class="space-y-2">
            {#if qualifyLibraries}
              <p
                class="text-xs font-medium uppercase tracking-wide text-muted-foreground"
              >
                {group.label} <span class="normal-case">(main)</span>
              </p>
            {/if}
            {#each group.libraries as library (`${group.key}:${library.libraryId}`)}
              {@const switchId = `scope-library-${group.key}-${library.libraryId}`}
              <div class="flex items-center gap-2">
                <Switch
                  id={switchId}
                  checked={selectedScopeLibraryIds.includes(library.libraryId)}
                  disabled={hasCustomLibraryCondition ||
                    evaluatingLibraryChange}
                  onCheckedChange={(checked) =>
                    void updateScopeLibrarySelection(
                      library.libraryId,
                      checked,
                    )}
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
                  <Label class="text-foreground" for={switchId}>
                    {library.libraryName}
                  </Label>
                </div>
              </div>
            {/each}
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
        Up to 10 groups total are supported per rule, with up to 4 levels of
        nesting.
      </p>
      {#if !hasValidConditions(definition.root)}
        <p class="text-xs text-amber-500 mt-1">
          Add at least one complete condition before saving.
        </p>
      {/if}
    </div>
    <div class="rounded-lg border border-border/60 bg-muted/20 p-2 md:p-5">
      <RuleNodeEditor
        node={definition.root}
        {targetScope}
        pathPickerMediaType={selectedMediaType}
        pathPickerLibraryIds={selectedPathScopeLibraryIds}
        onChange={() => (definition = { ...definition })}
      />
    </div>
  </div>

  {#if outcome === "candidate"}
    <div class="rounded-lg border border-border bg-card p-5 space-y-3">
      <div>
        <div class="flex items-center justify-between gap-3">
          <h3 class="font-semibold text-foreground">Automatic Deletion</h3>
          <Switch id="autoDeleteEnabled" bind:checked={autoDeleteEnabled} />
        </div>
        <p class="mt-1 text-sm text-muted-foreground">
          Enable this rule's candidates to be deleted by the scheduled
          <code>Delete Cleanup Candidates</code> task after their review period.
        </p>
      </div>
      {#if autoDeleteEnabled}
        <div class="space-y-2">
          <Label for="autoDeleteDelayDays" class="text-sm text-foreground">
            Delay in days
          </Label>
          <Input
            id="autoDeleteDelayDays"
            type="number"
            min="0"
            max="3650"
            step="1"
            bind:value={autoDeleteDelayDays}
            placeholder="Use default review period"
            class="max-w-xs"
          />
          <p class="text-xs text-muted-foreground">
            Leave blank to inherit the default {targetScope === "movie_version"
              ? "movie"
              : "TV"} review period. Use 0 for immediate eligibility. If multiple
            auto-delete-enabled rules match, the longest delay wins.
          </p>
        </div>
      {:else}
        <p class="text-xs text-muted-foreground">
          Matching candidates will appear in Candidates and Leaving Soon, but
          scheduled auto-delete will skip them until this rule is opted in.
        </p>
      {/if}
    </div>

    <div class="rounded-lg border border-border bg-card p-5 space-y-3">
      <div class="flex items-center justify-between gap-3">
        <div>
          <h3 class="font-semibold text-foreground">Move Instead of Delete</h3>
          <p class="mt-1 text-sm text-muted-foreground">
            Move this rule's candidates to the configured destination folder
            when a delete action runs. If disabled, delete actions remove files.
          </p>
        </div>
        <Switch id="moveInsteadOfDelete" bind:checked={moveInsteadOfDelete} />
      </div>
      <p class="text-xs text-muted-foreground">
        Destination folders are configured in General Settings. If multiple
        matched rules disagree, move wins over delete.
      </p>
    </div>

    <div class="rounded-lg border border-border bg-card p-5 space-y-4">
      <div>
        <h3 class="font-semibold text-foreground flex items-center gap-2">
          {#if selectedArrName === "Radarr"}
            <RadarrSVG class="size-4 inline" /> Radarr Configuration
          {:else if selectedArrName === "Sonarr"}
            <SonarrSVG class="size-4 inline" /> Sonarr Configuration
          {/if}
        </h3>
        <p class="mt-1 text-sm text-muted-foreground">
          Select one or more {selectedArrName} instances for managed tags and ARR
          actions. With none selected, deletion keeps automatic path-based routing
          across matching instances.
        </p>
      </div>

      <!-- instance -->
      <div class="space-y-2">
        <Label class="text-sm font-medium text-foreground"
          >{selectedArrName} Instances</Label
        >
        <div class="grid gap-2 sm:grid-cols-2">
          {#each selectedArrInstances as instance}
            <Label
              class="flex items-center gap-2 rounded-md border border-border bg-background/40 p-3 text-sm
                text-foreground cursor-pointer {instance.enabled
                ? ''
                : 'opacity-60'}"
            >
              <Checkbox
                checked={selectedArrConfigIds.includes(instance.id)}
                onCheckedChange={(checked) =>
                  toggleArrInstance(instance.id, checked === true)}
              />
              <span class="font-medium">
                {instance.name}{instance.enabled ? "" : " (disabled)"}
              </span>
            </Label>
          {/each}
        </div>
        {#if selectedArrInstances.length === 0}
          <p class="text-xs text-muted-foreground">
            No configured {selectedArrName} instances are available.
          </p>
        {:else if selectedArrConfigIds.length === 0}
          <p class="text-xs text-muted-foreground">
            Automatic routing is enabled; managed ARR tagging is disabled for
            this rule.
          </p>
        {/if}
      </div>

      {#if selectedArrConfigIds.length > 0}
        <!-- re-key the action control when the selected instance set changes -->
        <div class="space-y-2">
          <Label class="text-sm font-medium text-foreground">Action</Label>
          {#key `${targetScope}-${selectedArrConfigIds.join(",")}`}
            <Select.Root
              type="single"
              value={arrAction}
              onValueChange={(value) => {
                if (
                  value === "delete" ||
                  value === "unmonitor" ||
                  value === "unmonitor_only"
                ) {
                  if (targetScope === "movie_version") {
                    radarrArrAction = value;
                  } else {
                    sonarrArrAction = value;
                  }
                }
              }}
            >
              <Select.Trigger
                class="w-full bg-card text-card-foreground cursor-pointer"
              >
                {arrAction === "unmonitor"
                  ? "Unmonitor + Delete File"
                  : arrAction === "unmonitor_only"
                    ? "Unmonitor Only (Keep File)"
                    : "Delete"}
              </Select.Trigger>
              <Select.Content>
                <Select.Item value="delete" label="Delete">Delete</Select.Item>
                <Select.Item value="unmonitor" label="Unmonitor + Delete File">
                  Unmonitor + Delete File
                </Select.Item>
                <Select.Item
                  value="unmonitor_only"
                  label="Unmonitor Only (Keep File)"
                >
                  Unmonitor Only (Keep File)
                </Select.Item>
              </Select.Content>
            </Select.Root>
          {/key}
          {#if arrAction === "unmonitor"}
            <p class="text-xs text-muted-foreground">
              Files are deleted from disk but the entry remains in {selectedArrName}
              as unmonitored. Requires filesystem access on the Reclaimerr host.
            </p>
          {:else if arrAction === "unmonitor_only"}
            <p class="text-xs text-muted-foreground">
              The entry is set to unmonitored in {selectedArrName} and nothing is
              deleted - files are left on disk for manual review later.
            </p>
          {:else}
            <p class="text-xs text-muted-foreground">
              The {selectedArrName} entry and its files are fully removed.
            </p>
          {/if}
        </div>

        <!-- managed tag toggle -->
        <div class="space-y-3">
          <div class="flex items-center justify-between">
            <div>
              <p class="text-sm font-medium text-foreground">Managed Tag</p>
              <p class="text-xs text-muted-foreground">
                Apply a tag in {selectedArrName} to matched items.
              </p>
            </div>
            <Switch
              checked={tagEnabled}
              onCheckedChange={(value) => (tagEnabled = value)}
            />
          </div>

          {#if tagEnabled}
            <div class="space-y-1">
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
          {/if}
        </div>
      {/if}
    </div>
  {/if}
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
        {outcome === "protect"
          ? "Dry run only. Previewing does not save the rule or create protections."
          : "Dry run only. Previewing does not save the rule or create cleanup candidates."}
      </Dialog.Description>
    </Dialog.Header>

    <div class="space-y-4">
      <div class="flex flex-col sm:flex-row items-center justify-between gap-3">
        <div class="text-sm text-muted-foreground">
          {#if previewData}
            <strong>{previewData.total}</strong> matching item{previewData.total ===
            1
              ? ""
              : "s"}

            <!-- preview data -->
            {#if previewData.metadata}
              <div class="mt-1 text-xs text-muted-foreground italic">
                Evaluated <strong
                  >{previewData.metadata.source_media_count}</strong
                >
                active source item{previewData.metadata.source_media_count === 1
                  ? ""
                  : "s"} before applying library scope and other rule conditions.
                {#if previewData.metadata.skipped_favorites_count > 0 || previewData.metadata.skipped_protected_count > 0}
                  Excluded
                  {#if previewData.metadata.skipped_favorites_count > 0}
                    {" "}
                    <strong
                      >{previewData.metadata.skipped_favorites_count}</strong
                    >
                    favorite{previewData.metadata.skipped_favorites_count === 1
                      ? ""
                      : "s"}
                  {/if}
                  {#if previewData.metadata.skipped_favorites_count > 0 && previewData.metadata.skipped_protected_count > 0}
                    {" "}
                    and
                  {/if}
                  {#if previewData.metadata.skipped_protected_count > 0}
                    {" "}
                    <strong
                      >{previewData.metadata.skipped_protected_count}</strong
                    >
                    protected item{previewData.metadata
                      .skipped_protected_count === 1
                      ? ""
                      : "s"}
                  {/if}
                {/if}
              </div>
            {/if}
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
      {#if previewData?.metadata && previewData.metadata.seerr_unavailable}
        {@const unavailableInstances =
          previewData.metadata.seerr_unavailable_instances ?? []}
        <Notice type="warning" title="Seerr Data Unavailable">
          {#if unavailableInstances.length > 0}
            {unavailableInstances.join(", ")} could not be read. Every Seerr condition
            is answered from all configured instances at once, so one of them being
            unreachable makes the whole answer unknown rather than partly right.
          {:else}
            Seerr request data could not be loaded, so every Seerr condition was
            unknown and matched nothing.
          {/if}
          This preview is not a reliable answer.
          {#if previewData.metadata.seerr_error}
            {previewData.metadata.seerr_error}
          {/if}
        </Notice>
      {/if}
      {#if previewData?.metadata && previewData.metadata.requester_watch_unavailable_count > 0}
        <Notice type="warning" title="Requester Watch State Unavailable">
          {previewData.metadata.requester_watch_unavailable_count} item{previewData
            .metadata.requester_watch_unavailable_count === 1
            ? ""
            : "s"} sit on a media server whose watch state could not be read, so
          <strong>Seerr requester has watched</strong> is unknown for them and matched
          neither true nor false. Run a media sync or check that server before relying
          on this rule.
        </Notice>
      {/if}
      {#if previewData?.metadata && previewData.metadata.watch_completion_unavailable_count > 0}
        <Notice type="warning" title="Watch Completion Unavailable">
          {previewData.metadata.watch_completion_unavailable_count} item{previewData
            .metadata.watch_completion_unavailable_count === 1
            ? ""
            : "s"} sit on a media server whose watch state could not be read, so
          <strong>Fully watched by users</strong> is unknown for them and
          matched neither <em>matches any</em> nor <em>matches none</em>. Run a
          media sync or check that server before relying on this rule.
        </Notice>
      {/if}
      {#if previewData?.metadata && previewData.metadata.sonarr_unavailable_count > 0}
        <Notice type="warning" title="Sonarr Data Unavailable">
          Sonarr rule data could not be evaluated for
          {previewData.metadata.sonarr_unavailable_count}
          series. Those unknown values did not match.
          {#if previewData.metadata.sonarr_error}
            {previewData.metadata.sonarr_error}
          {/if}
        </Notice>
      {/if}
      {#if previewData?.metadata && previewData.metadata.season_inventory_unavailable_count > 0}
        <Notice type="warning" title="Season Inventory Unavailable">
          The current rule required Sonarr's episode inventory, but it was
          unavailable for
          {previewData.metadata.season_inventory_unavailable_count}
          evaluated item{previewData.metadata
            .season_inventory_unavailable_count === 1
            ? ""
            : "s"}. Only those were treated as unknown and did not match. Run
          Sync Media to refresh the inventory.
          {#if previewData.metadata.season_inventory_unavailable_examples.length > 0}
            Examples:
            {previewData.metadata.season_inventory_unavailable_examples.join(
              ", ",
            )}.
          {/if}
        </Notice>
      {/if}
      {#if previewData?.metadata && previewData.metadata.playback_unavailable_count > 0}
        <Notice type="warning" title="Playback Coverage">
          Of the active source items, playback data could not be observed for
          {previewData.metadata.playback_unavailable_count}
          media target{previewData.metadata.playback_unavailable_count === 1
            ? ""
            : "s"}. These are unknown values and match neither true nor false.
          {#if previewData.metadata.playback_error}
            {previewData.metadata.playback_error}
          {/if}
        </Notice>
      {/if}
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
                          {#if entry.episode_id !== null}
                            <span class="text-xs text-muted-foreground">
                              S{String(entry.season_number ?? 0).padStart(
                                2,
                                "0",
                              )}E{String(entry.episode_number ?? 0).padStart(
                                2,
                                "0",
                              )}
                              {#if entry.episode_name}
                                "{entry.episode_name}"
                              {/if}
                            </span>
                          {:else if entry.season_id !== null}
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
                      {#if ruleUsesRequesterWatch && entry.tmdb_id !== null}
                        <button
                          type="button"
                          class="text-xs leading-5 px-2 rounded-2xl border border-border bg-card text-muted-foreground hover:text-foreground cursor-pointer"
                          onclick={() => void openRequesterWatchExplain(entry)}
                        >
                          Why?
                        </button>
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

<!-- requester watch explanation dialog -->
<Dialog.Root bind:open={explainDialogOpen}>
  <Dialog.Content
    class="bg-card border border-border rounded-lg p-6 max-w-3xl w-full text-foreground max-h-[85vh] overflow-y-auto"
  >
    <Dialog.Header>
      <Dialog.Title class="text-xl font-semibold text-foreground mb-1">
        Seerr Requester Watch State
      </Dialog.Title>
      <Dialog.Description class="text-sm text-muted-foreground">
        Every identity tried and every completed watch found for this item.
      </Dialog.Description>
    </Dialog.Header>

    {#if explainLoading}
      <div class="py-8 text-center text-sm text-muted-foreground">
        Loading explanation...
      </div>
    {:else if explainError}
      <Notice type="error" title="Could Not Explain">{explainError}</Notice>
    {:else if explainData}
      <div class="space-y-4 text-sm">
        <div class="rounded-md border border-border bg-muted/20 p-3">
          <div class="font-medium">
            {explainData.title ?? `TMDB ${explainData.tmdb_id}`}
            <span class="text-muted-foreground">
              ({explainData.target_scope}{explainData.season_number !== null
                ? ` S${explainData.season_number}`
                : ""}{explainData.episode_number !== null
                ? `E${explainData.episode_number}`
                : ""})
            </span>
          </div>
          <div
            class="mt-1 {requesterWatchFieldsUsed.hasWatched
              ? ''
              : 'text-muted-foreground'}"
          >
            Seerr requester has watched:
            <strong>
              {explainData.result === null
                ? "unknown"
                : explainData.result
                  ? "true"
                  : "false"}
            </strong>
            {#if !requesterWatchFieldsUsed.hasWatched}
              <span>&mdash; not used by this rule</span>
            {/if}
          </div>
          <div
            class="mt-1 {requesterWatchFieldsUsed.afterRequest
              ? ''
              : 'text-muted-foreground'}"
          >
            Seerr requester watched after requesting:
            <strong>
              {explainData.result_after_request === null
                ? "unknown"
                : explainData.result_after_request
                  ? "true"
                  : "false"}
            </strong>
            {#if !requesterWatchFieldsUsed.afterRequest}
              <span>&mdash; not used by this rule</span>
            {:else if explainData.request_date_gate_ignored}
              <span class="text-muted-foreground">
                &mdash; request dates ignored (User Signals)
              </span>
            {/if}
          </div>
          <div class="mt-1 text-muted-foreground">{explainData.reason}</div>
        </div>

        {#if explainData.unobservable_services.length > 0}
          <Notice type="warning" title="Unreadable Media Server">
            {explainData.unobservable_services.join(", ")} could not report completion
            state, so this item cannot be judged.
          </Notice>
        {/if}

        <div>
          <div class="font-medium mb-1">Requesters</div>
          {#if explainData.requesters.length === 0}
            <div class="text-muted-foreground">
              No active Seerr request records a requester for this item.
            </div>
          {:else}
            <div class="space-y-2">
              {#each explainData.requesters as requester}
                <div class="rounded-md border border-border bg-muted/20 p-3">
                  <div>
                    {requester.display_name ??
                      `User ${requester.seerr_user_id}`}
                    <span class="text-muted-foreground">
                      (id {requester.seerr_user_id}{requester.service_name
                        ? ` on ${requester.service_name}`
                        : ""})
                    </span>
                  </div>
                  <div class="mt-1 text-muted-foreground break-all">
                    Seerr identities: {requester.identity_keys.join(", ") ||
                      "none"}
                  </div>
                  <div class="mt-1 text-muted-foreground">
                    First requested: {explainMoment(requester.requested_at)}
                  </div>
                  {#each Object.entries(requester.requested_seasons) as [season, requestedAt]}
                    <div class="mt-1 text-muted-foreground">
                      Season {season} requested: {explainMoment(requestedAt)}
                    </div>
                  {/each}
                  {#each Object.entries(requester.candidate_watch_keys) as [service, keys]}
                    <div class="mt-1 text-muted-foreground break-all">
                      Names tried on {service}: {keys.join(", ") || "none"}
                    </div>
                  {/each}
                  {#if explainData.media_type === MediaType.Movie}
                    {@const summary = movieRequesterWatchSummary(
                      requester,
                      explainData.request_date_gate_ignored,
                      requesterWatchFieldsUsed.afterRequest,
                    )}
                    <div
                      class="mt-1 break-all {summary.warning
                        ? 'text-amber-500'
                        : 'text-muted-foreground'}"
                    >
                      {summary.text}
                      {#if requester.movie_watched_at !== null}
                        ({explainMoment(requester.movie_watched_at)})
                      {/if}
                    </div>
                  {:else}
                    {#if requester.missing_episodes.length > 0}
                      <div class="mt-1 break-all text-amber-500">
                        Never watched ({requester.missing_episodes.length}): {requester.missing_episodes.join(
                          ", ",
                        )}
                      </div>
                    {/if}
                    {#if requester.episodes_watched_before_request.length > 0}
                      <div
                        class="mt-1 break-all {requestDateGateInPlay
                          ? 'text-amber-500'
                          : 'text-muted-foreground'}"
                      >
                        Watched before requesting ({requester
                          .episodes_watched_before_request.length}): {requester.episodes_watched_before_request.join(
                          ", ",
                        )}
                        {#if explainData.request_date_gate_ignored}
                          &mdash; still counted
                        {:else if !requesterWatchFieldsUsed.afterRequest}
                          &mdash; not used by this rule
                        {/if}
                      </div>
                    {/if}
                    {#if explainData.expected_episodes.length > 0 && requester.missing_episodes.length === 0 && requester.episodes_watched_before_request.length === 0}
                      <div class="mt-1 text-muted-foreground">
                        Watched every required episode, after requesting it.
                      </div>
                    {/if}
                  {/if}
                </div>
              {/each}
            </div>
          {/if}
        </div>

        <div>
          <div class="font-medium mb-1">Completed watches found</div>
          {#if explainData.evidence.length === 0}
            <div class="text-muted-foreground">
              No completed playback is recorded for this item.
            </div>
          {:else}
            <div class="space-y-2">
              {#each explainData.evidence as item}
                <div class="rounded-md border border-border bg-muted/20 p-3">
                  <div class="break-all">
                    <strong>{item.watch_user_key}</strong>
                    <span class="text-muted-foreground"
                      >on {item.source_service}, latest {explainMoment(
                        item.watched_at,
                      )}</span
                    >
                    {#if item.matched_requester_ids.length > 0}
                      <span class="text-muted-foreground">
                        &mdash; matched requester {item.matched_requester_ids.join(
                          ", ",
                        )}
                      </span>
                    {:else}
                      <span class="text-muted-foreground">
                        &mdash; not matched to any requester
                      </span>
                    {/if}
                  </div>
                  {#if item.episodes.length > 0}
                    <div class="mt-1 text-muted-foreground break-all">
                      {item.episodes.length} episode{item.episodes.length === 1
                        ? ""
                        : "s"}: {item.episodes.join(", ")}
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          {/if}
        </div>

        {#if explainData.expected_episodes.length > 0}
          <div>
            <div class="font-medium mb-1">
              Episodes required ({explainData.expected_episodes.length})
            </div>
            <div class="text-muted-foreground break-all">
              {explainData.expected_episodes.join(", ")}
            </div>
          </div>
        {/if}
      </div>
    {/if}
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
            This library scope change invalidates all current path/filename
            criteria. Confirming will clear those conditions from this rule.
          </p>
        {:else}
          <p>
            This library scope change invalidates some path/filename criteria.
            Confirming will remove only the invalid values and keep the rest.
          </p>
        {/if}
        {#if pendingInvalidPaths.length > 0}
          <div
            class="rounded-md border border-border bg-muted/30 p-3 max-h-44 overflow-y-auto"
          >
            <p
              class="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2"
            >
              Invalid Criteria ({pendingInvalidPaths.length})
            </p>
            <ul class="space-y-1.5">
              {#each pendingInvalidPaths as criterion}
                <li class="font-mono text-xs break-all text-foreground/90">
                  {criterion.field}
                  {criterion.operator}
                  {criterion.value}
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
