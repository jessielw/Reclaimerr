<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import { get_api } from "$lib/api";
  import { onMount } from "svelte";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import Notice from "$lib/components/notice.svelte";
  import JellyfinSVG from "$lib/components/svgs/JellyfinSVG.svelte";
  import PlexSVG from "$lib/components/svgs/PlexSVG.svelte";
  import ArrowLeft from "@lucide/svelte/icons/arrow-left";
  import Save from "@lucide/svelte/icons/save";
  import RuleNodeEditor from "$lib/components/settings/rules/rule-node-editor.svelte";
  import {
    MediaType,
    SettingsTab,
    type LibraryType,
    type ReclaimRule,
    type RuleCondition,
    type RuleDefinition,
    type RuleNode,
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
      children: [
        {
          type: "condition",
          field: "media.size",
          operator: "greater_than",
          value: 0,
        },
      ],
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

  const DEFAULT_LIBRARY_CONDITION: RuleCondition = {
    type: "condition",
    field: "media.size",
    operator: "greater_than",
    value: 0,
  };

  // normalize library ids from a condition value, ensuring it's always an array of non empty strings
  const normalizeLibraryIds = (value: RuleCondition["value"]) =>
    (Array.isArray(value) ? value : [value])
      .filter((id): id is string | number => id !== null && id !== undefined)
      .map((id) => String(id).trim())
      .filter(Boolean);

  // recursively collect all library.id conditions in the rule tree
  const collectLibraryConditions = (node: RuleNode): RuleCondition[] => {
    if (node.type === "condition") {
      return node.field === "library.id" ? [node] : [];
    }
    return node.children.flatMap(collectLibraryConditions);
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
    if (hasCustomLibraryCondition) return;
    const next = selected
      ? [...selectedScopeLibraryIds, libraryId]
      : selectedScopeLibraryIds.filter((id) => id !== libraryId);
    const filtered = next.filter((id) => scopeLibraryIds.has(id));
    selectedScopeLibraryIds = Array.from(new Set(filtered));
    applyCanonicalLibraryScope(selectedScopeLibraryIds);
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
            children: [DEFAULT_LIBRARY_CONDITION],
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
        library_ids: null,
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
  <div class="flex items-center justify-between gap-3">
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
    <Button
      onclick={save}
      disabled={saving || !name.trim()}
      class="gap-2 cursor-pointer"
    >
      <Save class="size-4" />
      {saving ? "Saving..." : "Save Rule"}
    </Button>
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
          class="input-hover-el"
          bind:value={name}
          placeholder="Rule name"
        />
      </div>

      <!-- target -->
      <div class="space-y-2 w-full">
        <Label class="text-sm font-medium text-foreground">Target</Label>
        <Select.Root type="single" bind:value={targetScope}>
          <Select.Trigger class="w-full flex-10 bg-card text-card-foreground">
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
              disabled={hasCustomLibraryCondition}
              onCheckedChange={(checked) =>
                updateScopeLibrarySelection(library.libraryId, checked)}
            />
            <div class="flex items-center gap-1.5">
              <div class="w-4 h-4 shrink-0">
                {#if library.serviceType === SettingsTab.Jellyfin}
                  <JellyfinSVG />
                {:else if library.serviceType === SettingsTab.Plex}
                  <PlexSVG />
                {/if}
              </div>
              <Label for={`scope-library-${library.libraryId}`}>
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
    </div>
    <div class="rounded-lg border border-border/60 bg-muted/20 p-4 md:p-5">
      <RuleNodeEditor
        node={definition.root}
        onChange={() => (definition = { ...definition })}
      />
    </div>
  </div>

  <div class="rounded-lg border border-border bg-card p-5">
    <div class="flex items-center justify-between gap-3">
      <div class="w-full">
        <div class="flex justify-between w-full">
          <h3 class="font-semibold text-foreground">{selectedArrName}</h3>
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
            <Select.Trigger class="w-full flex-10 bg-card text-card-foreground">
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
