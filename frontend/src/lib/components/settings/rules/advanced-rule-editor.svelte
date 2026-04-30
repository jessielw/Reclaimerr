<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import { get_api } from "$lib/api";
  import { onMount } from "svelte";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import ArrowLeft from "@lucide/svelte/icons/arrow-left";
  import Save from "@lucide/svelte/icons/save";
  import RuleNodeEditor from "$lib/components/settings/rules/rule-node-editor.svelte";
  import {
    MediaType,
    type LibraryType,
    type ReclaimRule,
    type RuleDefinition,
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

  const selectedLibraries = $derived(
    libraries.filter((library) => library.mediaType === selectedMediaType),
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

  // allowed (lowercase letters, numbers, dashes, underscores)
  function sanitizeTagInput(value: string): string {
    // remove rec- if user tries to type it
    let v = value.replace(/^rec-/, "");
    // return empty if nothing left after removing disallowed chars
    if (!v) return "";
    // remove disallowed characters
    v = v.replace(/[^a-z-_]/g, "");
    // truncate to fit within 25 chars including 'rec-'
    v = v.slice(0, 25 - 4);
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

  const addLibraryCondition = (libraryId: string) => {
    const existing = definition.root.children.find(
      (child) => child.type === "condition" && child.field === "library.id",
    );
    if (existing && existing.type === "condition") {
      const values = Array.isArray(existing.value)
        ? existing.value.map((value) => String(value))
        : [];
      existing.value = Array.from(new Set([...values, libraryId]));
    } else {
      definition.root.children = [
        {
          type: "condition",
          field: "library.id",
          operator: "contains_any",
          value: [libraryId],
        },
        ...definition.root.children,
      ];
    }
    definition = { ...definition };
  };

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
        Add library conditions when a rule should only run against specific
        libraries.
      </p>
    </div>
    {#if selectedLibraries.length > 0}
      <div class="flex flex-wrap gap-2">
        {#each selectedLibraries as library}
          <Button
            size="sm"
            variant="secondary"
            onclick={() => addLibraryCondition(library.libraryId)}
          >
            {library.libraryName}
          </Button>
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
