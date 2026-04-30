<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import Plus from "@lucide/svelte/icons/plus";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import Self from "$lib/components/settings/rules/rule-node-editor.svelte";
  import type {
    RuleCondition,
    RuleConditionOperator,
    RuleGroup,
    RuleNode,
  } from "$lib/types/shared";

  interface Props {
    node: RuleNode;
    rootNode?: RuleNode;
    depth?: number;
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

  let {
    node,
    rootNode = node,
    depth = 0,
    onChange,
    onRemove,
  }: Props = $props();

  const operatorLabelMap: Record<RuleConditionOperator, string> = {
    equals: "is",
    not_equals: "is not",
    greater_than: "greater than",
    greater_than_or_equal: ">=",
    less_than: "less than",
    less_than_or_equal: "<=",
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

  const temporalOperators: RuleConditionOperator[] = ["exists", "not_exists"];

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
  ];

  const MAX_TOTAL_GROUPS = 5;

  const fieldConfig = (fieldValue: string) =>
    fields.find((field) => field.value === fieldValue) ?? fields[0];

  const operatorOptions = (fieldValue: string) =>
    fieldConfig(fieldValue).operators.map((value) => ({
      value,
      label: operatorLabelMap[value],
    }));

  const ensureValidOperator = (condition: RuleCondition) => {
    const config = fieldConfig(condition.field);
    if (config.operators.includes(condition.operator)) return;
    condition.operator = config.defaultOperator;
  };

  const addCondition = (group: RuleGroup) => {
    group.children = [
      ...group.children,
      {
        type: "condition",
        field: "media.size",
        operator: "greater_than",
        value: 0,
      },
    ];
    onChange();
  };

  const addGroup = (group: RuleGroup) => {
    group.children = [
      ...group.children,
      {
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
    ];
    onChange();
  };

  const removeChild = (group: RuleGroup, index: number) => {
    group.children = group.children.filter((_, i) => i !== index);
    onChange();
  };

  const applyConditionValue = (condition: RuleCondition, raw: string) => {
    if (valuelessOperators.has(condition.operator)) {
      delete condition.value;
      return;
    }

    if (listOperators.has(condition.operator)) {
      condition.value = raw
        .split(",")
        .map((v) => v.trim())
        .filter(Boolean);
      return;
    }

    if (fieldConfig(condition.field).kind === "number") {
      if (raw === "") {
        condition.value = null;
        return;
      }
      const parsed = Number(raw);
      condition.value = Number.isFinite(parsed) ? parsed : null;
      return;
    }

    condition.value = raw;
  };

  const setConditionValue = (condition: RuleCondition, raw: string) => {
    applyConditionValue(condition, raw);
    onChange();
  };

  const setConditionField = (condition: RuleCondition, fieldValue: string) => {
    const raw = valueText(condition);
    condition.field = fieldValue;
    ensureValidOperator(condition);
    applyConditionValue(condition, raw);
    onChange();
  };

  const setConditionOperator = (
    condition: RuleCondition,
    operatorValue: RuleConditionOperator,
  ) => {
    const raw = valueText(condition);
    condition.operator = operatorValue;
    ensureValidOperator(condition);
    applyConditionValue(condition, raw);
    onChange();
  };

  const valueText = (condition: RuleCondition) => {
    const value = condition.value;
    if (Array.isArray(value)) return value.join(", ");
    return value === null || value === undefined ? "" : String(value);
  };

  const fieldLabel = (value: string) =>
    fields.find((f) => f.value === value)?.label ?? value;

  const operatorLabel = (value: RuleConditionOperator) =>
    operatorLabelMap[value] ?? value;

  const isNumericInput = (condition: RuleCondition) =>
    fieldConfig(condition.field).kind === "number" &&
    !listOperators.has(condition.operator);

  const valuePlaceholder = (condition: RuleCondition) => {
    if (condition.operator === "matches_any_regex") return "regex patterns...";
    if (listOperators.has(condition.operator)) return "comma-separated...";
    return "value...";
  };

  const countGroups = (current: RuleNode): number => {
    if (current.type !== "group") return 0;
    return (
      1 + current.children.reduce((sum, child) => sum + countGroups(child), 0)
    );
  };

  const canAddGroup = (root: RuleNode) => countGroups(root) < MAX_TOTAL_GROUPS;

  // Hide "Add Group" if this group already contains a child group
  const hasChildGroup = (group: RuleGroup) =>
    group.children.some((child) => child.type === "group");

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

  const depthIndent = (d: number) => `${d * 12}px`;
  const mobileDepthIndent = (d: number) => `${d * 6}px`;
</script>

{#if node.type === "group"}
  <div>
    <div class="md:hidden" style={`padding-left: ${mobileDepthIndent(depth)}`}>
      <div
        class={`rounded-lg border border-border/70 ${groupBg(depth)} overflow-hidden`}
      >
        <!-- group header -->
        <div class="px-3 py-2.5 border-b border-border/60 space-y-2">
          <div class="flex items-center justify-between gap-2">
            <div
              class="flex items-center rounded-md border border-border overflow-hidden shrink-0"
            >
              <button
                class={`px-2.5 py-1 text-xs font-bold transition-colors cursor-pointer ${
                  node.op === "and"
                    ? "bg-primary text-primary-foreground"
                    : "bg-transparent text-muted-foreground hover:text-foreground"
                }`}
                onclick={() => {
                  node.op = "and";
                  onChange();
                }}
              >
                AND
              </button>
              <button
                class={`px-2.5 py-1 text-xs font-bold transition-colors cursor-pointer ${
                  node.op === "or"
                    ? "bg-primary text-primary-foreground"
                    : "bg-transparent text-muted-foreground hover:text-foreground"
                }`}
                onclick={() => {
                  node.op = "or";
                  onChange();
                }}
              >
                OR
              </button>
            </div>
            {#if onRemove}
              <Button
                size="icon-sm"
                class="cursor-pointer bg-destructive/80 hover:bg-destructive/90 text-destructive-foreground"
                onclick={onRemove}
              >
                <Trash2 class="size-3.5" />
              </Button>
            {/if}
          </div>
          <span class="block text-xs text-muted-foreground">
            {node.op === "and"
              ? "All conditions must match"
              : "Any condition can match"}
          </span>
        </div>

        <!-- children -->
        <div class="p-3 space-y-2">
          {#each node.children as child, index}
            <Self
              node={child}
              {rootNode}
              depth={depth + 1}
              {onChange}
              onRemove={() => removeChild(node, index)}
            />
          {/each}
        </div>

        <!-- footer actions -->
        <div class="flex flex-col items-stretch gap-2 px-3 pb-3">
          <Button
            size="sm"
            variant="secondary"
            class="h-9 text-xs gap-1.5 cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground w-full"
            onclick={() => addCondition(node)}
          >
            <Plus class="size-3.5" />
            Add condition
          </Button>

          {#if !hasChildGroup(node)}
            <Button
              size="sm"
              variant="secondary"
              class="h-9 text-xs gap-1.5 cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground w-full"
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
            <span class="text-xs text-muted-foreground">
              Max {MAX_TOTAL_GROUPS} groups reached
            </span>
          {/if}
        </div>
      </div>
    </div>

    <div class="hidden md:block" style={`padding-left: ${depthIndent(depth)}`}>
      <div
        class={`rounded-lg border border-border/70 ${groupBg(depth)} overflow-hidden`}
      >
        <!-- group header -->
        <div
          class="flex items-center gap-3 px-4 py-2.5 border-b border-border/60"
        >
          <!-- AND / OR toggle -->
          <div
            class="flex items-center rounded-md border border-border overflow-hidden shrink-0"
          >
            <button
              class={`px-2.5 py-1 text-xs font-bold transition-colors cursor-pointer ${
                node.op === "and"
                  ? "bg-primary text-primary-foreground"
                  : "bg-transparent text-muted-foreground hover:text-foreground"
              }`}
              onclick={() => {
                node.op = "and";
                onChange();
              }}
            >
              AND
            </button>
            <button
              class={`px-2.5 py-1 text-xs font-bold transition-colors cursor-pointer ${
                node.op === "or"
                  ? "bg-primary text-primary-foreground"
                  : "bg-transparent text-muted-foreground hover:text-foreground"
              }`}
              onclick={() => {
                node.op = "or";
                onChange();
              }}
            >
              OR
            </button>
          </div>

          <span class="text-xs text-muted-foreground grow">
            {node.op === "and"
              ? "All conditions must match"
              : "Any condition can match"}
          </span>

          {#if onRemove}
            <Button
              size="icon-sm"
              class="cursor-pointer bg-destructive/80 hover:bg-destructive/90 text-destructive-foreground"
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
              {onChange}
              onRemove={() => removeChild(node, index)}
            />
          {/each}
        </div>

        <!-- footer actions -->
        <div class="flex items-center gap-2 px-3 pb-3">
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
            <span class="text-xs text-muted-foreground ml-1">
              Max {MAX_TOTAL_GROUPS} groups reached
            </span>
          {/if}
        </div>
      </div>
    </div>
  </div>
{:else}
  <!-- condition row -->
  <div
    class={`rounded-md border border-border/70 px-3 py-2.5 ${conditionBg(depth)}`}
    style={`margin-left: ${mobileDepthIndent(Math.max(depth - 1, 0))}`}
  >
    <div class="md:hidden space-y-2.5">
      <Select.Root
        type="single"
        value={node.field}
        onValueChange={(value) => setConditionField(node, value)}
      >
        <Select.Trigger
          class="h-9 min-w-0 w-full text-sm text-foreground cursor-pointer bg-background"
        >
          {fieldLabel(node.field)}
        </Select.Trigger>
        <Select.Content>
          {#each fields as field}
            <Select.Item value={field.value} label={field.label}
              >{field.label}</Select.Item
            >
          {/each}
        </Select.Content>
      </Select.Root>

      <Select.Root
        type="single"
        value={node.operator}
        onValueChange={(value) =>
          setConditionOperator(node, value as RuleConditionOperator)}
      >
        <Select.Trigger
          class="h-9 min-w-0 w-full text-sm text-foreground cursor-pointer bg-background"
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

      {#if !valuelessOperators.has(node.operator)}
        <Input
          class="h-9 min-w-0 w-full text-sm input-hover-el text-foreground placeholder:text-muted-foreground bg-background"
          type={isNumericInput(node) ? "number" : "text"}
          placeholder={valuePlaceholder(node)}
          value={valueText(node)}
          oninput={(e) => setConditionValue(node, e.currentTarget.value)}
        />
      {/if}

      {#if onRemove}
        <div class="flex justify-end pt-1">
          <Button
            size="icon-sm"
            class="cursor-pointer bg-destructive/80 hover:bg-destructive/90 text-destructive-foreground"
            onclick={onRemove}
          >
            <Trash2 class="size-3.5" />
          </Button>
        </div>
      {/if}
    </div>

    <div
      class="hidden md:flex md:flex-nowrap items-center gap-2"
      style={`margin-left: ${depthIndent(Math.max(depth - 1, 0))}`}
    >
      <!-- field -->
      <Select.Root
        type="single"
        value={node.field}
        onValueChange={(value) => setConditionField(node, value)}
      >
        <Select.Trigger
          class="h-8 min-w-0 flex-1 w-full text-sm text-foreground cursor-pointer bg-background"
        >
          {fieldLabel(node.field)}
        </Select.Trigger>
        <Select.Content>
          {#each fields as field}
            <Select.Item value={field.value} label={field.label}
              >{field.label}</Select.Item
            >
          {/each}
        </Select.Content>
      </Select.Root>

      <!-- operator -->
      <Select.Root
        type="single"
        value={node.operator}
        onValueChange={(value) =>
          setConditionOperator(node, value as RuleConditionOperator)}
      >
        <Select.Trigger
          class="h-8 min-w-0 flex-1 w-full text-sm text-foreground cursor-pointer bg-background"
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

      <!-- value (hidden when valueless operator) -->
      {#if !valuelessOperators.has(node.operator)}
        <Input
          class="h-8 min-w-0 flex-1 text-sm input-hover-el text-foreground placeholder:text-muted-foreground bg-background"
          type={isNumericInput(node) ? "number" : "text"}
          placeholder={valuePlaceholder(node)}
          value={valueText(node)}
          oninput={(e) => setConditionValue(node, e.currentTarget.value)}
        />
      {:else}
        <div class="flex-1 hidden md:block"></div>
      {/if}

      <!-- remove -->
      {#if onRemove}
        <Button
          size="icon-sm"
          class="cursor-pointer bg-destructive/80 hover:bg-destructive/90 text-destructive-foreground"
          onclick={onRemove}
        >
          <Trash2 class="size-3.5" />
        </Button>
      {/if}
    </div>
  </div>
{/if}
