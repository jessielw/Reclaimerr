<script lang="ts">
  import { onMount, type Component } from "svelte";
  import { get_api, post_api, delete_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import Plus from "@lucide/svelte/icons/plus";
  import Pencil from "@lucide/svelte/icons/pencil";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import Download from "@lucide/svelte/icons/download";
  import Upload from "@lucide/svelte/icons/upload";
  import Clapperboard from "@lucide/svelte/icons/clapperboard";
  import Tv from "@lucide/svelte/icons/tv";
  import { toast } from "svelte-sonner";
  import {
    MediaType,
    SettingsTab,
    type ReclaimRule,
    type LibraryType,
    type RuleNode,
  } from "$lib/types/shared";
  import AdvancedRuleEditor from "$lib/components/settings/rules/advanced-rule-editor.svelte";
  import Notice from "$lib/components/notice.svelte";

  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  let loading = $state(false);
  let rules = $state<ReclaimRule[]>([]);
  let editingRule = $state<ReclaimRule | null>(null);
  let showRuleForm = $state(false);
  let availableLibraries = $state<LibraryType[]>([]);

  // delete dialog states
  let showDeleteDialog = $state(false);
  let ruleToDelete = $state<ReclaimRule | null>(null);

  // import/export states
  interface ImportPreviewItem {
    originalName: string;
    resolvedName: string;
    isRenamed: boolean;
  }
  let fileInput = $state<HTMLInputElement>(null!);
  let showImportDialog = $state(false);
  let importLoading = $state(false);
  let importPreviewItems = $state<ImportPreviewItem[]>([]);
  let importRules = $state<any[]>([]);
  let hasArrServiceConfigWarning = $state(false);

  // additional states
  let isSynced = $state(true); // assume synced until we check

  const toPlainRule = (rule: ReclaimRule): ReclaimRule => {
    try {
      return structuredClone(rule);
    } catch {
      return JSON.parse(JSON.stringify(rule)) as ReclaimRule;
    }
  };

  // load rules from API
  const loadRules = async () => {
    try {
      loading = true;
      const response = await get_api<ReclaimRule[]>("/api/rules");
      rules = response;
    } catch (err: any) {
      toast.error(`Error loading rules: ${err.message}`);
    } finally {
      loading = false;
    }
  };

  const loadLibraries = async () => {
    try {
      const services = await get_api<any>("/api/settings/services");
      const libraries: LibraryType[] = [];

      if (services.jellyfin?.libraries) {
        services.jellyfin.libraries.forEach((lib: any) => {
          libraries.push({
            id: lib.id,
            libraryId: lib.library_id,
            libraryName: lib.library_name,
            mediaType:
              lib.media_type === "movie" ? MediaType.Movie : MediaType.Series,
            serviceType: SettingsTab.Jellyfin,
            selected: lib.selected,
          });
        });
      }

      if (services.plex?.libraries) {
        services.plex.libraries.forEach((lib: any) => {
          libraries.push({
            id: lib.id,
            libraryId: lib.library_id,
            libraryName: lib.library_name,
            mediaType:
              lib.media_type === "movie" ? MediaType.Movie : MediaType.Series,
            serviceType: SettingsTab.Plex,
            selected: lib.selected,
          });
        });
      }

      availableLibraries = libraries.sort((a, b) =>
        a.libraryName.localeCompare(b.libraryName),
      );
    } catch (err: any) {
      toast.error(`Error loading libraries: ${err.message}`);
    }
  };

  // save rule (create or update)
  const handleSaveRule = async (ruleData: Partial<ReclaimRule>) => {
    try {
      if (editingRule) {
        // update existing rule
        const updated = await post_api<ReclaimRule>(
          `/api/rules/${editingRule.id}`,
          ruleData,
        );
        rules = rules.map((r) => (r.id === updated.id ? updated : r));
        toast.success(`Rule "${updated.name}" updated`);
      } else {
        // create new rule
        const created = await post_api<ReclaimRule>("/api/rules", ruleData);
        rules = [...rules, created];
        toast.success(`Rule "${created.name}" created`);
      }
      closeRuleForm();
    } catch (err: any) {
      toast.error(`Error saving rule: ${err.message}`);
      throw err; // re-throw to prevent form from closing
    }
  };

  // toggle rule enabled/disabled
  const toggleRuleEnabled = async (rule: ReclaimRule) => {
    try {
      const updatedRule = { ...rule, enabled: !rule.enabled };
      await post_api(`/api/rules/${rule.id}`, updatedRule);
      rule.enabled = !rule.enabled;
      toast.success(
        `Rule "${rule.name}" ${rule.enabled ? "enabled" : "disabled"}`,
      );
    } catch (err: any) {
      toast.error(`Error updating rule: ${err.message}`);
    }
  };

  // delete rule
  const deleteRule = async (rule: ReclaimRule) => {
    try {
      await delete_api(`/api/rules/${rule.id}`);
      rules = rules.filter((r) => r.id !== rule.id);
      toast.success(`Rule "${rule.name}" deleted`);
    } catch (err: any) {
      toast.error(`Error deleting rule: ${err.message}`);
    }
  };

  // open form to edit existing rule
  const editRule = (rule: ReclaimRule) => {
    editingRule = toPlainRule(rule);
    showRuleForm = true;
  };

  // open form to create new rule
  const createNewRule = () => {
    editingRule = null;
    showRuleForm = true;
  };

  // close rule form modal
  const closeRuleForm = () => {
    showRuleForm = false;
    editingRule = null;
  };

  // generate summary text for a rule based on its conditions
  const getRuleSummary = (rule: ReclaimRule): string => {
    if (!rule.definition?.root) return "No conditions set";

    const countConditions = (node: RuleNode): number => {
      if (node.type === "condition") return 1;
      return node.children.reduce(
        (count, child) => count + countConditions(child),
        0,
      );
    };
    const count = countConditions(rule.definition.root);
    const target =
      rule.target_scope === "movie_version"
        ? "movie versions"
        : rule.target_scope === "season"
          ? "seasons"
          : "series";
    return `${count} condition${count === 1 ? "" : "s"} targeting ${target}`;
  };

  // open delete confirmation dialog
  const openDeleteDialog = (rule: ReclaimRule) => {
    ruleToDelete = rule;
    showDeleteDialog = true;
  };

  // close delete confirmation dialog
  const closeDeleteDialog = () => {
    showDeleteDialog = false;
    // small delay to allow dialog close animation before removing value from state
    setTimeout(() => {
      ruleToDelete = null;
    }, 200);
  };

  // confirm delete rule
  const confirmDelete = async () => {
    if (ruleToDelete) {
      await deleteRule(ruleToDelete);
      closeDeleteDialog();
    }
  };

  // check if server has completed initial sync of media data - if not, rules may not work
  // correctly so we show a warning
  const checkIfServerSynced = async () => {
    try {
      const response = await get_api<{
        libraries: number;
        movies: number;
        series: number;
      }>("/api/rules/check-synced");
      if (
        response.libraries === 0 ||
        response.movies === 0 ||
        response.series === 0
      ) {
        isSynced = false;
      } else {
        isSynced = true;
      }
    } catch (err: any) {
      console.error("Error checking sync status:", err);
    }
  };

  // export rules to a JSON file (strips server assigned fields)
  const exportRules = (rulesToExport: ReclaimRule[]) => {
    const exportData = rulesToExport.map((rule) => ({
      name: rule.name,
      media_type: rule.media_type,
      enabled: rule.enabled,
      target_scope: rule.target_scope,
      definition: rule.definition,
      action: rule.action,
    }));
    const blob = new Blob([JSON.stringify(exportData, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download =
      rulesToExport.length === 1
        ? `rule-${rulesToExport[0].name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}.json`
        : "reclaimerr-rules.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  // handle file selection for import
  const handleImportFile = async (event: Event) => {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    // reset so the same file can be re-selected
    input.value = "";
    if (!file) return;

    try {
      const text = await file.text();
      const parsed: unknown = JSON.parse(text);
      const rawRules: any[] = Array.isArray(parsed) ? parsed : [parsed];

      if (rawRules.length === 0) {
        toast.error("No rules found in file");
        return;
      }
      for (const rule of rawRules) {
        if (!rule.name || !rule.definition || !rule.target_scope) {
          toast.error(
            "Invalid rule format: each rule must have name, definition, and target_scope",
          );
          return;
        }
      }

      const existingNames = new Set(rules.map((r) => r.name));
      const usedNames = new Set(existingNames);
      const previewItems: ImportPreviewItem[] = [];
      const resolvedRules: any[] = [];
      let hasArrWarning = false;

      for (const rule of rawRules) {
        // strip any server assigned fields that may have been included
        const { id: _id, created_at: _ca, updated_at: _ua, ...ruleData } = rule;

        let resolvedName: string = ruleData.name;
        if (usedNames.has(resolvedName)) {
          let candidate = `${ruleData.name} (imported)`;
          let n = 2;
          while (usedNames.has(candidate)) {
            candidate = `${ruleData.name} (imported ${n})`;
            n++;
          }
          resolvedName = candidate;
        }
        usedNames.add(resolvedName);

        if (
          rule.action?.radarr_service_config_id != null ||
          rule.action?.sonarr_service_config_id != null
        ) {
          hasArrWarning = true;
        }

        previewItems.push({
          originalName: rule.name,
          resolvedName,
          isRenamed: resolvedName !== rule.name,
        });
        resolvedRules.push({ ...ruleData, name: resolvedName });
      }

      importPreviewItems = previewItems;
      importRules = resolvedRules;
      hasArrServiceConfigWarning = hasArrWarning;
      showImportDialog = true;
    } catch (err: any) {
      toast.error(`Failed to parse file: ${err.message}`);
    }
  };

  // send the resolved rules to the backend
  const confirmImport = async () => {
    importLoading = true;
    try {
      const result = await post_api<{ imported: number; errors: string[] }>(
        "/api/rules/import",
        { rules: importRules },
      );
      showImportDialog = false;
      importPreviewItems = [];
      importRules = [];
      await loadRules();
      if (result.errors.length > 0) {
        toast.warning(
          `Imported ${result.imported} rule${result.imported === 1 ? "" : "s"} ` +
            `with ${result.errors.length} error${result.errors.length === 1 ? "" : "s"}`,
        );
      } else {
        toast.success(
          `Imported ${result.imported} rule${result.imported === 1 ? "" : "s"}`,
        );
      }
    } catch (err: any) {
      toast.error(`Import failed: ${err.message}`);
    } finally {
      importLoading = false;
    }
  };

  onMount(async () => {
    await Promise.all([loadRules(), loadLibraries()]);

    // check to see if there are any media to display - if not, show a warning that rules may
    // not work correctly until initial sync is complete
    setTimeout(async () => {
      await checkIfServerSynced();
    }, 100);
  });
</script>

{#if loading}
  <div class="flex items-center justify-center gap-3 text-muted-foreground p-8">
    <Spinner class="size-5 text-primary" />
    Loading...
  </div>
{:else if showRuleForm}
  <AdvancedRuleEditor
    rule={editingRule}
    libraries={availableLibraries}
    onSave={handleSaveRule}
    onCancel={closeRuleForm}
  />
{:else}
  <div class="space-y-6">
    <div class="flex justify-between items-center mb-3">
      <!-- header -->
      <div>
        <h2
          class="flex items-center gap-3 text-xl font-semibold text-foreground"
        >
          {#if svgIcon}
            {@const Icon = svgIcon}
            <Icon class="size-5" aria-hidden="true" />
          {/if}
          <span class="align-middle">Rules</span>
        </h2>
        <p class="text-sm text-muted-foreground mt-1">Manage cleanup rules</p>
      </div>

      <!-- action buttons -->
      <div class="flex items-center gap-2">
        <input
          type="file"
          accept=".json"
          class="hidden"
          bind:this={fileInput}
          onchange={handleImportFile}
        />
        <!-- import rule button -->
        <Button
          variant="secondary"
          size="sm"
          onclick={() => fileInput.click()}
          class="cursor-pointer gap-2"
        >
          <Upload class="size-4" />
          Import
        </Button>

        <!-- export rule button -->
        <Button
          variant="secondary"
          size="sm"
          onclick={() => exportRules(rules)}
          disabled={rules.length === 0}
          class="cursor-pointer gap-2"
        >
          <Download class="size-4" />
          Export All
        </Button>
        <Button size="sm" onclick={createNewRule} class="cursor-pointer gap-2">
          <Plus class="size-4" />
          New Rule
        </Button>
      </div>
    </div>

    <div class="max-w-7xl mx-auto space-y-4">
      <!-- sync status -->
      {#if !isSynced}
        <Notice type="warning" title="Missing Media Data">
          Media data is still syncing or hasn't been synced yet. Rules may not
          work correctly until sync is complete. You can start the sync if not
          already in progress in <strong>Tasks</strong>, or wait and check back
          later.
        </Notice>
      {/if}

      <!-- new rule info -->
      <!-- loading -->
      {#if loading}
        <div
          class="flex p-8 items-center justify-center text-center gap-3 text-muted-foreground"
        >
          <Spinner class="size-5 text-primary" />
          Loading rules...
        </div>
      {:else if rules.length === 0}
        <div class="bg-card rounded-lg border border-border p-12 text-center">
          <div class="max-w-md mx-auto">
            <h3 class="text-lg font-semibold text-foreground mb-2">
              No rules yet
            </h3>
            <p class="text-muted-foreground">
              Create your first cleanup rule to start identifying media
              candidates for removal
            </p>
          </div>
        </div>

        <!-- loaded -->
      {:else}
        <div class="space-y-4">
          {#each rules as rule}
            <div
              class="bg-card rounded-lg border border-border p-6 hover:shadow-md transition-shadow"
            >
              <div class="flex items-start justify-between gap-4">
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-3 mb-2">
                    <h3 class="text-lg font-semibold text-foreground truncate">
                      {rule.name}
                    </h3>
                  </div>
                  <div class="flex items-center gap-3 mb-2">
                    <Badge
                      variant="secondary"
                      class="shrink-0 {rule.media_type === MediaType.Movie
                        ? 'border-movie'
                        : 'border-series'}"
                    >
                      <div class="inline-flex items-center gap-1.5">
                        {#if rule.media_type === MediaType.Movie}
                          <Clapperboard class="size-4" />
                        {:else}
                          <Tv class="size-4" />
                        {/if}
                        <span class="capitalize">
                          {rule.target_scope === "movie_version"
                            ? "Movie version"
                            : (rule.target_scope ?? rule.media_type)}
                        </span>
                      </div>
                    </Badge>
                    <Badge
                      variant={rule.enabled ? "default" : "secondary"}
                      class="shrink-0"
                    >
                      {rule.enabled ? "Enabled" : "Disabled"}
                    </Badge>
                  </div>
                  <p class="text-sm text-muted-foreground line-clamp-2">
                    {getRuleSummary(rule)}
                  </p>
                </div>

                <div class="flex items-center gap-2 shrink-0">
                  <Switch
                    checked={rule.enabled}
                    onCheckedChange={() => toggleRuleEnabled(rule)}
                    class="cursor-pointer"
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    onclick={() => editRule(rule)}
                    class="text-foreground cursor-pointer"
                  >
                    <Pencil class="size-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    onclick={() => exportRules([rule])}
                    class="text-foreground cursor-pointer"
                    title="Export rule"
                  >
                    <Download class="size-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    onclick={() => openDeleteDialog(rule)}
                    class="cursor-pointer text-destructive hover:text-destructive"
                  >
                    <Trash2 class="size-4" />
                  </Button>
                </div>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </div>
{/if}

<!-- import confirmation dialog -->
{#if showImportDialog}
  <AlertDialog.Root
    open={showImportDialog}
    onOpenChange={(v) => {
      if (!importLoading) showImportDialog = v;
    }}
  >
    <AlertDialog.Content
      class="bg-card border border-border rounded-lg p-6 max-w-lg w-full"
    >
      <AlertDialog.Header>
        <AlertDialog.Title class="text-xl font-semibold text-foreground mb-2"
          >Import Rules</AlertDialog.Title
        >
        <AlertDialog.Description class="text-muted-foreground">
          The following {importPreviewItems.length}
          {importPreviewItems.length === 1 ? "rule" : "rules"} will be imported.
        </AlertDialog.Description>
      </AlertDialog.Header>

      {#if hasArrServiceConfigWarning}
        <Notice type="warning" title="Arr Service Configuration">
          One or more rules reference a Radarr or Sonarr service from the
          original instance. Those IDs may not match your setup — review rule
          actions after importing.
        </Notice>
      {/if}

      <div class="mt-4 max-h-64 overflow-y-auto space-y-2">
        {#each importPreviewItems as item}
          <div
            class="flex items-center gap-2 rounded-md border border-border p-3 text-sm"
          >
            <div class="flex-1 min-w-0">
              {#if item.isRenamed}
                <p class="text-muted-foreground line-through truncate">
                  {item.originalName}
                </p>
                <p class="text-foreground font-medium truncate">
                  {item.resolvedName}
                </p>
              {:else}
                <p class="text-foreground truncate">{item.resolvedName}</p>
              {/if}
            </div>
            {#if item.isRenamed}
              <Badge variant="secondary" class="shrink-0 text-xs">Renamed</Badge
              >
            {/if}
          </div>
        {/each}
      </div>

      <AlertDialog.Footer class="flex justify-end gap-3 pt-4">
        <Button
          variant="secondary"
          class="cursor-pointer"
          disabled={importLoading}
          onclick={() => {
            showImportDialog = false;
            importPreviewItems = [];
            importRules = [];
          }}
        >
          Cancel
        </Button>
        <Button
          class="cursor-pointer gap-2"
          disabled={importLoading}
          onclick={confirmImport}
        >
          {#if importLoading}
            <Spinner class="size-4" />
          {/if}
          Import {importPreviewItems.length}
          {importPreviewItems.length === 1 ? "Rule" : "Rules"}
        </Button>
      </AlertDialog.Footer>
    </AlertDialog.Content>
  </AlertDialog.Root>
{/if}

<AlertDialog.Root
  open={showDeleteDialog}
  onOpenChange={(v) => (showDeleteDialog = v)}
>
  <AlertDialog.Content
    class="bg-card border border-border rounded-lg p-6 max-w-md w-full"
  >
    <AlertDialog.Header>
      <AlertDialog.Title class="text-xl font-semibold text-foreground mb-2"
        >Delete Rule</AlertDialog.Title
      >
      <AlertDialog.Description class="text-muted-foreground">
        Are you sure you want to delete rule <span class="font-semibold"
          >{ruleToDelete?.name}</span
        >? This action cannot be undone.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer class="flex justify-end gap-3 pt-4">
      <AlertDialog.Cancel
        class="cursor-pointer hover text-foreground bg-secondary"
        onclick={closeDeleteDialog}
      >
        Cancel
      </AlertDialog.Cancel>
      <AlertDialog.Action class="cursor-pointer hover" onclick={confirmDelete}>
        Delete
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
