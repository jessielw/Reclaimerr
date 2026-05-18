<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import PathPatternPicker from "$lib/components/settings/rules/path-pattern-picker.svelte";
  import FolderSearch from "@lucide/svelte/icons/folder-search";
  import Plus from "@lucide/svelte/icons/plus";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import Self from "$lib/components/settings/rules/rule-node-editor.svelte";
  import type {
    MediaType,
    RuleCondition,
    RuleConditionOperator,
    RuleGroup,
    RuleNode,
  } from "$lib/types/shared";

  interface Props {
    node: RuleNode;
    rootNode?: RuleNode;
    depth?: number;
    pathPickerMediaType?: MediaType;
    pathPickerLibraryIds?: string[] | null;
    onChange: () => void;
    onRemove?: () => void;
  }

  type FieldKind = "number" | "text" | "boolean" | "temporal";

  interface FieldConfig {
    value: string;
    label: string;
    kind: FieldKind;
    operators: RuleConditionOperator[];
    defaultOperator: RuleConditionOperator;
  }

  interface FieldGroup {
    key: string;
    label: string;
    items: FieldConfig[];
  }

  let {
    node,
    rootNode = node,
    depth = 0,
    pathPickerMediaType = undefined,
    pathPickerLibraryIds = null,
    onChange,
    onRemove,
  }: Props = $props();

  let pathPickerOpen = $state(false);

  const operatorLabelMap: Record<RuleConditionOperator, string> = {
    equals: "is",
    not_equals: "is not",
    greater_than: "greater than",
    greater_than_or_equal: ">=",
    less_than: "less than",
    less_than_or_equal: "<=",
    before: "before",
    on_or_before: "on or before",
    after: "after",
    on_or_after: "on or after",
    in: "in any",
    not_in: "not in any",
    contains_any: "contains any",
    not_contains_any: "excludes all",
    exists: "exists",
    not_exists: "does not exist",
    is_true: "is true",
    is_false: "is false",
    matches_any_regex: "matches regex",
  };

  const listOperators = new Set<RuleConditionOperator>([
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "matches_any_regex",
  ]);

  const valuelessOperators = new Set<RuleConditionOperator>([
    "exists",
    "not_exists",
    "is_true",
    "is_false",
  ]);

  const numericOperators: RuleConditionOperator[] = [
    "equals",
    "not_equals",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "exists",
    "not_exists",
  ];

  const textOperators: RuleConditionOperator[] = [
    "equals",
    "not_equals",
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "exists",
    "not_exists",
  ];

  const libraryOperators: RuleConditionOperator[] = [
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "exists",
    "not_exists",
  ];

  const pathOperators: RuleConditionOperator[] = [
    ...textOperators,
    "matches_any_regex",
  ];

  const booleanOperators: RuleConditionOperator[] = [
    "is_true",
    "is_false",
    "exists",
    "not_exists",
  ];

  const temporalOperators: RuleConditionOperator[] = [
    "exists",
    "not_exists",
    "before",
    "on_or_before",
    "after",
    "on_or_after",
  ];

  const fields: FieldConfig[] = [
    {
      value: "library.id",
      label: "Library",
      kind: "text",
      operators: libraryOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "media.path",
      label: "Path",
      kind: "text",
      operators: pathOperators,
      defaultOperator: "matches_any_regex",
    },
    {
      value: "media.file_name",
      label: "Filename",
      kind: "text",
      operators: pathOperators,
      defaultOperator: "matches_any_regex",
    },
    {
      value: "media.size",
      label: "Size (bytes)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than",
    },
    {
      value: "media.days_since_added",
      label: "Days since added",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "watch.view_count",
      label: "View count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "watch.days_since_last_watched",
      label: "Days since watched",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "watch.last_viewed_at",
      label: "Last watched",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "tmdb.release_date",
      label: "TMDB release date",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "tmdb.first_air_date",
      label: "TMDB first air date",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "tmdb.last_air_date",
      label: "TMDB last air date",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "season.air_date",
      label: "Season air date",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "tmdb.days_since_release",
      label: "Days since released",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "tmdb.days_since_first_air_date",
      label: "Days since first aired",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "tmdb.days_since_last_air_date",
      label: "Days since last aired",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "season.days_since_air_date",
      label: "Days since season aired",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "season.season_number",
      label: "Season number",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "equals",
    },
    {
      value: "season.episode_count",
      label: "Episode count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "less_than",
    },
    {
      value: "season.is_latest_season",
      label: "Is latest season",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "season.seasons_from_latest",
      label: "Seasons from latest",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "episode.number",
      label: "Episode number",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "equals",
    },
    {
      value: "episode.season_number",
      label: "Episode season number",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "equals",
    },
    {
      value: "episode.air_date",
      label: "Episode air date",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "before",
    },
    {
      value: "episode.days_since_air_date",
      label: "Days since episode aired",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "watch.never_watched",
      label: "Never watched",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "tmdb.popularity",
      label: "TMDB popularity",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "tmdb.vote_average",
      label: "TMDB rating",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "tmdb.vote_count",
      label: "TMDB votes",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "series.status",
      label: "Series status",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "video.codec_family",
      label: "Video codec family",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "audio.codec_family",
      label: "Audio codec family",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "video.hdr",
      label: "HDR",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "video.dolby_vision",
      label: "Dolby Vision",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "video.width",
      label: "Video width",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "video.height",
      label: "Video height",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "video.resolution",
      label: "Resolution",
      kind: "text",
      operators: textOperators,
      defaultOperator: "equals",
    },
    {
      value: "audio.channels",
      label: "Audio channels",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "audio.track_count",
      label: "Audio track count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "audio.languages",
      label: "Audio languages",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "subtitle.languages",
      label: "Subtitle languages",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "video.color_space",
      label: "Video color space",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "video.color_transfer",
      label: "Video color transfer",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "video.color_primaries",
      label: "Video color primaries",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "media.duration",
      label: "Duration (ms)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "arr.tags",
      label: "Arr tags",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "arr.monitored",
      label: "Arr monitored",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "seerr.requested",
      label: "Seerr requested",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "disk.free_bytes",
      label: "Disk free (bytes)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "less_than",
    },
    {
      value: "disk.free_percent",
      label: "Disk free (%)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "less_than",
    },
  ];

  // organize fields into common and categorized groups for the UI
  const COMMON_FIELD_VALUES = new Set<string>([
    "library.id",
    "media.path",
    "media.size",
    "watch.never_watched",
    "watch.view_count",
    "watch.days_since_last_watched",
    "watch.last_viewed_at",
    "series.status",
    "tmdb.vote_average",
    "disk.free_percent",
  ]);

  const fieldLabelComparator = (a: FieldConfig, b: FieldConfig) =>
    a.label.localeCompare(b.label, undefined, { sensitivity: "base" });

  const categoryLabel = (fieldValue: string): string => {
    const [prefix] = fieldValue.split(".");
    switch (prefix) {
      case "library":
        return "Library";
      case "media":
        return "Media";
      case "watch":
        return "Watch";
      case "tmdb":
        return "TMDB";
      case "season":
        return "Season";
      case "episode":
        return "Episode";
      case "series":
        return "Series";
      case "video":
        return "Video";
      case "audio":
        return "Audio";
      case "subtitle":
        return "Subtitle";
      case "arr":
        return "Arr";
      case "seerr":
        return "Seerr";
      case "disk":
        return "Disk";
      default:
        return "Other";
    }
  };

  const groupedFields: FieldGroup[] = (() => {
    const groups: FieldGroup[] = [];

    const commonItems = fields
      .filter((field) => COMMON_FIELD_VALUES.has(field.value))
      .sort(fieldLabelComparator);
    if (commonItems.length > 0) {
      groups.push({ key: "common", label: "Common", items: commonItems });
    }

    const byCategory = new Map<string, FieldConfig[]>();
    for (const field of fields) {
      if (COMMON_FIELD_VALUES.has(field.value)) continue;
      const label = categoryLabel(field.value);
      const current = byCategory.get(label) ?? [];
      current.push(field);
      byCategory.set(label, current);
    }

    for (const [label, items] of [...byCategory.entries()].sort((a, b) =>
      a[0].localeCompare(b[0], undefined, { sensitivity: "base" }),
    )) {
      groups.push({
        key: label.toLowerCase(),
        label,
        items: items.sort(fieldLabelComparator),
      });
    }

    return groups;
  })();

  const MAX_TOTAL_GROUPS = 5;

  const TMDB_SERIES_STATUSES = [
    "Returning Series",
    "Ended",
    "Canceled",
    "In Production",
    "Planned",
    "Pilot",
  ];

  // helpers
  const fieldConfig = (fieldValue: string) =>
    fields.find((f) => f.value === fieldValue) ?? fields[0];

  const operatorOptions = (fieldValue: string) =>
    fieldConfig(fieldValue).operators.map((value) => ({
      value,
      label: operatorLabelMap[value],
    }));

  const fieldLabel = (value: string) =>
    fields.find((f) => f.value === value)?.label ?? value;
  const operatorLabel = (value: RuleConditionOperator) =>
    operatorLabelMap[value] ?? value;
  const isNumericInput = (c: RuleCondition) =>
    fieldConfig(c.field).kind === "number" && !listOperators.has(c.operator);
  const isTemporalInput = (c: RuleCondition) =>
    fieldConfig(c.field).kind === "temporal" &&
    !valuelessOperators.has(c.operator);
  const valuePlaceholder = (c: RuleCondition) => {
    if (c.operator === "matches_any_regex") return "regex patterns…";
    if (listOperators.has(c.operator)) return "comma-separated…";
    return "value…";
  };

  const valueText = (c: RuleCondition) => {
    const v = c.value;
    if (Array.isArray(v)) return v.join(", ");
    return v === null || v === undefined ? "" : String(v);
  };

  const normalizeValueList = (value: RuleCondition["value"]): string[] =>
    (Array.isArray(value) ? value : value == null ? [] : [value])
      .map((item) => String(item).trim())
      .filter(Boolean);

  const countGroups = (n: RuleNode): number =>
    n.type !== "group"
      ? 0
      : 1 + n.children.reduce((s, c) => s + countGroups(c), 0);

  const canAddGroup = (root: RuleNode) => countGroups(root) < MAX_TOTAL_GROUPS;
  const hasChildGroup = (group: RuleGroup) =>
    group.children.some((c) => c.type === "group");

  // mutations
  const ensureValidOperator = (c: RuleCondition) => {
    const config = fieldConfig(c.field);
    if (!config.operators.includes(c.operator))
      c.operator = config.defaultOperator;
  };

  const applyConditionValue = (c: RuleCondition, raw: string) => {
    if (valuelessOperators.has(c.operator)) {
      delete c.value;
      return;
    }
    if (listOperators.has(c.operator)) {
      c.value = raw
        .split(",")
        .map((v) => v.trim())
        .filter(Boolean);
      return;
    }
    if (fieldConfig(c.field).kind === "number") {
      if (raw === "") {
        c.value = null;
        return;
      }
      const parsed = Number(raw);
      c.value = Number.isFinite(parsed) ? parsed : null;
      return;
    }
    c.value = raw;
  };

  const setConditionValue = (c: RuleCondition, raw: string) => {
    applyConditionValue(c, raw);
    onChange();
  };

  const setConditionField = (c: RuleCondition, fieldValue: string) => {
    const raw = valueText(c);
    c.field = fieldValue;
    ensureValidOperator(c);
    // don't carry numeric defaults into path conditions
    if (fieldValue === "media.path") {
      if (valuelessOperators.has(c.operator)) delete c.value;
      else if (listOperators.has(c.operator)) c.value = [];
      else c.value = "";
    } else {
      applyConditionValue(c, raw);
    }
    onChange();
  };

  const setConditionOperator = (
    c: RuleCondition,
    op: RuleConditionOperator,
  ) => {
    const raw = valueText(c);
    c.operator = op;
    ensureValidOperator(c);
    applyConditionValue(c, raw);
    onChange();
  };

  const addCondition = (group: RuleGroup) => {
    group.children = [
      ...group.children,
      {
        type: "condition",
        field: "media.size",
        operator: "greater_than",
        value: null,
      },
    ];
    onChange();
  };

  const addGroup = (group: RuleGroup) => {
    group.children = [
      ...group.children,
      { type: "group", op: "and", children: [] },
    ];
    onChange();
  };

  const removeChild = (group: RuleGroup, index: number) => {
    group.children = group.children.filter((_, i) => i !== index);
    onChange();
  };

  const addPathPattern = (c: RuleCondition, pattern: string) => {
    const cleaned = pattern.trim();
    if (!cleaned) return;
    if (listOperators.has(c.operator)) {
      const existing = normalizeValueList(c.value);
      if (!existing.includes(cleaned)) {
        c.value = [...existing, cleaned];
        onChange();
      }
      return;
    }
    c.value = cleaned;
    onChange();
  };

  // styling
  const groupBg = (d: number) =>
    [
      "bg-card",
      "bg-cyan-50/80 dark:bg-cyan-950/30",
      "bg-emerald-50/80 dark:bg-emerald-950/30",
      "bg-amber-50/75 dark:bg-amber-950/28",
      "bg-violet-50/75 dark:bg-violet-950/28",
    ][Math.min(d, 4)];

  const conditionBg = (d: number) =>
    [
      "bg-background",
      "bg-cyan-50/45 dark:bg-cyan-950/20",
      "bg-emerald-50/45 dark:bg-emerald-950/20",
      "bg-amber-50/45 dark:bg-amber-950/18",
      "bg-violet-50/45 dark:bg-violet-950/18",
    ][Math.min(d, 4)];

  // single indent scale (halved on mobile via the CSS variable approach below)
  const indentPx = (d: number) => `${d * 12}px`;
</script>

{#if node.type === "group"}
  <div style={`padding-left: ${indentPx(depth)}`}>
    <div
      class={`rounded-lg border border-border/70 ${groupBg(depth)} overflow-hidden`}
    >
      <!-- group header -->
      <div
        class="flex flex-wrap items-center gap-2 px-4 py-2.5 border-b border-border/60"
      >
        <!-- AND / OR toggle -->
        <div
          class="flex items-center rounded-md border border-border overflow-hidden shrink-0"
        >
          {#each ["and", "or"] as const as op}
            <button
              class={`px-2.5 py-1 text-xs font-bold transition-colors cursor-pointer ${
                node.op === op
                  ? "bg-primary text-primary-foreground"
                  : "bg-transparent text-muted-foreground hover:text-foreground"
              }`}
              onclick={() => {
                node.op = op;
                onChange();
              }}
            >
              {op.toUpperCase()}
            </button>
          {/each}
        </div>

        <span class="text-xs text-muted-foreground grow">
          {node.op === "and"
            ? "All conditions must match"
            : "Any condition can match"}
        </span>

        {#if onRemove}
          <Button
            size="icon-sm"
            class="cursor-pointer bg-destructive/80 hover:bg-destructive/90 text-destructive-foreground shrink-0"
            onclick={onRemove}
          >
            <Trash2 class="size-3.5" />
          </Button>
        {/if}
      </div>

      <!-- children -->
      <div class="p-3 space-y-2">
        {#each node.children as child, index}
          <Self
            node={child}
            {rootNode}
            depth={depth + 1}
            {pathPickerMediaType}
            {pathPickerLibraryIds}
            {onChange}
            onRemove={() => removeChild(node, index)}
          />
        {/each}
      </div>

      <!-- footer actions -->
      <div class="flex flex-wrap items-center gap-2 px-3 pb-3">
        <Button
          size="sm"
          variant="secondary"
          class="h-8 text-xs gap-1.5 cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground"
          onclick={() => addCondition(node)}
        >
          <Plus class="size-3.5" />
          Add condition
        </Button>

        {#if !hasChildGroup(node)}
          <Button
            size="sm"
            variant="secondary"
            class="h-8 text-xs gap-1.5 cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground"
            onclick={() => addGroup(node)}
            disabled={!canAddGroup(rootNode)}
            title={!canAddGroup(rootNode)
              ? `Max ${MAX_TOTAL_GROUPS} groups`
              : undefined}
          >
            <Plus class="size-3.5" />
            Add group
          </Button>
        {/if}

        {#if !canAddGroup(rootNode)}
          <span class="text-xs text-muted-foreground"
            >Max {MAX_TOTAL_GROUPS} groups reached</span
          >
        {/if}
      </div>
    </div>
  </div>
{:else}
  <!-- condition row -->
  <div
    class={`rounded-md border border-border/70 px-3 py-2.5 ${conditionBg(depth)}`}
    style={`margin-left: ${indentPx(Math.max(depth - 1, 0))}`}
  >
    <!--
      Layout strategy (attempt to keep things nice):
      - stack vertically on mobile (flex-col)
      - switch to a wrapping row on sm+ (flex-row flex-wrap)
      - value + browse share a row via their own inner flex so browse never 
      gets squeezed out at awkward breakpoints
    -->
    <div
      class="flex flex-col sm:flex-row sm:flex-wrap items-stretch sm:items-center gap-2"
    >
      <!-- field selector -->
      <Select.Root
        type="single"
        value={node.field}
        onValueChange={(value) => setConditionField(node, value)}
      >
        <Select.Trigger
          class="h-8 w-full sm:w-auto sm:flex-1 sm:min-w-36 text-sm text-foreground cursor-pointer bg-background"
        >
          {fieldLabel(node.field)}
        </Select.Trigger>
        <!-- organize fields into common and categorized groups for the UI -->
        <Select.Content>
          {#each groupedFields as group, index (group.key)}
            <Select.Group>
              <Select.GroupHeading>{group.label}</Select.GroupHeading>
              {#each group.items as field (field.value)}
                <Select.Item value={field.value} label={field.label}
                  >{field.label}</Select.Item
                >
              {/each}
            </Select.Group>
            {#if index < groupedFields.length - 1}
              <Select.Separator />
            {/if}
          {/each}
        </Select.Content>
      </Select.Root>

      <!-- operator selector -->
      <Select.Root
        type="single"
        value={node.operator}
        onValueChange={(value) =>
          setConditionOperator(node, value as RuleConditionOperator)}
      >
        <Select.Trigger
          class="h-8 w-full sm:w-auto sm:flex-1 sm:min-w-32 text-sm text-foreground cursor-pointer bg-background"
        >
          {operatorLabel(node.operator)}
        </Select.Trigger>
        <Select.Content>
          {#each operatorOptions(node.field) as op}
            <Select.Item value={op.value} label={op.label}
              >{op.label}</Select.Item
            >
          {/each}
        </Select.Content>
      </Select.Root>

      <!-- value + optional browse button - always on their own flex row so browse can't get pushed off 
        screen by the selects above -->
      {#if !valuelessOperators.has(node.operator)}
        <div class="flex items-center gap-2 w-full sm:flex-1 sm:min-w-40">
          {#if node.field === "series.status" && !listOperators.has(node.operator)}
            <Select.Root
              type="single"
              value={typeof node.value === "string" ? node.value : ""}
              onValueChange={(v) => setConditionValue(node, v)}
            >
              <Select.Trigger
                class="h-8 flex-1 text-sm text-foreground cursor-pointer bg-background"
              >
                {typeof node.value === "string" && node.value
                  ? node.value
                  : "Select status…"}
              </Select.Trigger>
              <Select.Content>
                {#each TMDB_SERIES_STATUSES as status}
                  <Select.Item value={status} label={status}
                    >{status}</Select.Item
                  >
                {/each}
              </Select.Content>
            </Select.Root>
          {:else}
            <Input
              class="h-8 flex-1 min-w-0 text-sm text-foreground placeholder:text-muted-foreground bg-background"
              type={isNumericInput(node)
                ? "number"
                : isTemporalInput(node)
                  ? "date"
                  : "text"}
              placeholder={valuePlaceholder(node)}
              value={valueText(node)}
              oninput={(e) => setConditionValue(node, e.currentTarget.value)}
            />
            {#if node.field === "media.path" && pathPickerMediaType}
              <Button
                size="sm"
                variant="secondary"
                class="h-8 text-xs gap-1.5 cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground shrink-0"
                onclick={() => (pathPickerOpen = true)}
              >
                <FolderSearch class="size-3.5" />
                <span class="hidden sm:inline">Browse</span>
              </Button>
            {/if}
          {/if}
        </div>
      {:else}
        <!-- spacer so the remove button stays right aligned on desktop -->
        <div class="hidden sm:flex sm:flex-1"></div>
      {/if}

      <!-- remove button -->
      {#if onRemove}
        <Button
          size="icon-sm"
          class="cursor-pointer bg-destructive/80 hover:bg-destructive/90 text-destructive-foreground self-end sm:self-center shrink-0"
          onclick={onRemove}
        >
          <Trash2 class="size-3.5" />
        </Button>
      {/if}
    </div>
  </div>

  {#if node.field === "media.path" && pathPickerMediaType}
    <PathPatternPicker
      bind:open={pathPickerOpen}
      mediaType={pathPickerMediaType}
      libraryIds={pathPickerLibraryIds}
      onSelect={(pattern) => addPathPattern(node, pattern)}
    />
  {/if}
{/if}
