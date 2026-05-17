<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { get_api, post_api, delete_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import Plus from "@lucide/svelte/icons/plus";
  import Pencil from "@lucide/svelte/icons/pencil";
  import Copy from "@lucide/svelte/icons/copy";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import Download from "@lucide/svelte/icons/download";
  import Upload from "@lucide/svelte/icons/upload";
  import Play from "@lucide/svelte/icons/play";
  import Clapperboard from "@lucide/svelte/icons/clapperboard";
  import Tv from "@lucide/svelte/icons/tv";
  import { toast } from "svelte-sonner";
  import {
    MediaType,
    SettingsTab,
    type ReclaimRule,
    type LibraryType,
    type RuleNode,
    TaskStatus,
  } from "$lib/types/shared";
  import AdvancedRuleEditor from "$lib/components/settings/rules/advanced-rule-editor.svelte";
  import Notice from "$lib/components/notice.svelte";
  import { auth } from "$lib/stores/auth";
  import { getTaskStatusText } from "$lib/utils/tasks";

  let loading = $state(false);
  let rules = $state<ReclaimRule[]>([]);
  let editingRule = $state<ReclaimRule | null>(null);
  let ruleFormMode = $state<"create" | "edit">("create");
  let showRuleForm = $state(false);
  let availableLibraries = $state<LibraryType[]>([]);

  let showDeleteDialog = $state(false);
  let ruleToDelete = $state<ReclaimRule | null>(null);

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

  let isSynced = $state(true);
  const SCAN_TASK_ID = "scan_cleanup_candidates";
  const SCAN_TASK_POLL_INTERVAL_MS = 8000;
  let scanTaskStatus = $state<TaskStatus | null>(null);
  let scanTaskError = $state<string | null>(null);
  let scanTaskActionInProgress = $state(false);
  let scanTaskPollInterval: number | null = null;

  interface TaskStatusResponse {
    status: TaskStatus;
    error: string | null;
  }

  interface RunTaskResponse {
    already_active: boolean;
  }

  const toPlainRule = (rule: ReclaimRule): ReclaimRule => {
    try {
      return structuredClone(rule);
    } catch {
      return JSON.parse(JSON.stringify(rule)) as ReclaimRule;
    }
  };

  const loadRules = async () => {
    try {
      loading = true;
      rules = await get_api<ReclaimRule[]>("/api/rules");
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

  const handleSaveRule = async (ruleData: Partial<ReclaimRule>) => {
    try {
      if (ruleFormMode === "edit" && editingRule) {
        const updated = await post_api<ReclaimRule>(
          `/api/rules/${editingRule.id}`,
          ruleData,
        );
        rules = rules.map((r) => (r.id === updated.id ? updated : r));
        toast.success(`Rule "${updated.name}" updated`);
      } else {
        const created = await post_api<ReclaimRule>("/api/rules", ruleData);
        rules = [...rules, created];
        toast.success(`Rule "${created.name}" created`);
      }
      closeRuleForm();
    } catch (err: any) {
      toast.error(`Error saving rule: ${err.message}`);
      throw err;
    }
  };

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

  const deleteRule = async (rule: ReclaimRule) => {
    try {
      await delete_api(`/api/rules/${rule.id}`);
      rules = rules.filter((r) => r.id !== rule.id);
      toast.success(`Rule "${rule.name}" deleted`);
    } catch (err: any) {
      toast.error(`Error deleting rule: ${err.message}`);
    }
  };

  const editRule = (rule: ReclaimRule) => {
    editingRule = toPlainRule(rule);
    ruleFormMode = "edit";
    showRuleForm = true;
  };

  const cloneRule = (rule: ReclaimRule) => {
    const cloned = toPlainRule(rule);
    cloned.name = `${rule.name} (copy)`;
    editingRule = cloned;
    ruleFormMode = "create";
    showRuleForm = true;
  };

  const createNewRule = () => {
    editingRule = null;
    ruleFormMode = "create";
    showRuleForm = true;
  };

  const closeRuleForm = () => {
    showRuleForm = false;
    editingRule = null;
    ruleFormMode = "create";
  };

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

  const openDeleteDialog = (rule: ReclaimRule) => {
    ruleToDelete = rule;
    showDeleteDialog = true;
  };

  const closeDeleteDialog = () => {
    showDeleteDialog = false;
    setTimeout(() => {
      ruleToDelete = null;
    }, 200);
  };

  const confirmDelete = async () => {
    if (ruleToDelete) {
      await deleteRule(ruleToDelete);
      closeDeleteDialog();
    }
  };

  const checkIfServerSynced = async () => {
    try {
      const response = await get_api<{
        libraries: number;
        movies: number;
        series: number;
      }>("/api/rules/check-synced");
      isSynced =
        response.libraries !== 0 &&
        response.movies !== 0 &&
        response.series !== 0;
    } catch (err: any) {
      console.error("Error checking sync status:", err);
    }
  };

  const fetchScanTaskStatus = async () => {
    try {
      const response = await get_api<TaskStatusResponse>(
        `/api/tasks/tasks/${SCAN_TASK_ID}`,
      );
      scanTaskStatus = response.status;
      scanTaskError = response.error;
    } catch (err) {
      console.error("Error checking scan task status:", err);
    }
  };

  const runScanTaskNow = async () => {
    scanTaskActionInProgress = true;
    try {
      const response = await post_api<RunTaskResponse>(
        `/api/tasks/tasks/${SCAN_TASK_ID}/run`,
      );
      if (response.already_active) {
        toast.message("Scan is already queued or running");
      } else {
        toast.success("Cleanup candidate scan queued");
      }
      await fetchScanTaskStatus();
    } catch (err: any) {
      toast.error(`Failed to start scan: ${err.message}`);
    } finally {
      scanTaskActionInProgress = false;
    }
  };

  const startScanTaskStatusPolling = () => {
    if (scanTaskPollInterval) clearInterval(scanTaskPollInterval);
    scanTaskPollInterval = window.setInterval(
      fetchScanTaskStatus,
      SCAN_TASK_POLL_INTERVAL_MS,
    );
  };

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

  const handleImportFile = async (event: Event) => {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
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
          `Imported ${result.imported} rule${result.imported === 1 ? "" : "s"} with ${result.errors.length} error${result.errors.length === 1 ? "" : "s"}`,
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
    await Promise.all([loadRules(), loadLibraries(), fetchScanTaskStatus()]);
    startScanTaskStatusPolling();
    setTimeout(async () => {
      await checkIfServerSynced();
    }, 100);
  });

  onDestroy(() => {
    if (scanTaskPollInterval) clearInterval(scanTaskPollInterval);
  });
</script>

<div class="p-2.5 md:p-8">
  <div class="max-w-7xl mx-auto">
    {#if $auth.user?.role !== "admin"}
      <div
        class="bg-card rounded-lg border border-border p-8 text-center text-muted-foreground"
      >
        You do not have access to this page.
      </div>
    {:else if loading}
      <div
        class="flex items-center justify-center gap-3 text-muted-foreground p-8"
      >
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
      <div class="space-y-4">
        <div>
          <h1 class="text-3xl font-bold text-foreground">Rules</h1>
          <p class="text-muted-foreground">Manage cleanup rules</p>
        </div>

        <div class="flex justify-between items-center mb-3">
          <div class="flex items-center flex-wrap gap-3 text-sm">
            <span class="text-muted-foreground">Scan task:</span>
            <Button
              size="sm"
              onclick={runScanTaskNow}
              disabled={scanTaskActionInProgress}
              class="cursor-pointer gap-2"
              title="Start cleanup candidate scan"
            >
              {#if scanTaskActionInProgress}
                <Spinner class="size-4" />
              {:else}
                <Play class="size-4" />
              {/if}
              {getTaskStatusText(scanTaskStatus)}
            </Button>
            {#if scanTaskError}
              <span class="text-destructive truncate" title={scanTaskError}>
                {scanTaskError}
              </span>
            {/if}
          </div>

          <div class="flex flex-wrap justify-end items-center gap-2">
            <input
              type="file"
              accept=".json"
              class="hidden"
              bind:this={fileInput}
              onchange={handleImportFile}
            />
            <Button
              variant="secondary"
              size="sm"
              onclick={() => fileInput.click()}
              class="cursor-pointer gap-2"
            >
              <Upload class="size-4" />
              Import
            </Button>
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
            <Button
              size="sm"
              onclick={createNewRule}
              class="cursor-pointer gap-2"
            >
              <Plus class="size-4" />
              New Rule
            </Button>
          </div>
        </div>

        <div class="space-y-4">
          {#if !isSynced}
            <Notice type="warning" title="Missing Media Data">
              Media data is still syncing or hasn't been synced yet. Rules may
              not work correctly until sync is complete. You can start the sync
              if not already in progress in <strong>Tasks</strong>, or wait and
              check back later.
            </Notice>
          {/if}

          {#if rules.length === 0}
            <div
              class="bg-card rounded-lg border border-border p-12 text-center"
            >
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
          {:else}
            <div class="space-y-4">
              {#each rules as rule}
                <div
                  class="bg-card rounded-lg border border-border p-6 hover:shadow-md transition-shadow"
                >
                  <div class="flex items-start justify-between gap-4">
                    <div class="flex-1 min-w-0">
                      <div class="flex items-center gap-3 mb-2">
                        <h3
                          class="text-lg font-semibold text-foreground truncate"
                        >
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
                        title="Edit rule"
                      >
                        <Pencil class="size-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        onclick={() => cloneRule(rule)}
                        class="text-foreground cursor-pointer"
                        title="Clone rule"
                      >
                        <Copy class="size-4" />
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
  </div>
</div>

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
        <AlertDialog.Title class="text-xl font-semibold text-foreground mb-2">
          Import Rules
        </AlertDialog.Title>
        <AlertDialog.Description class="text-muted-foreground">
          The following {importPreviewItems.length}
          {importPreviewItems.length === 1 ? "rule" : "rules"} will be imported.
        </AlertDialog.Description>
      </AlertDialog.Header>

      {#if hasArrServiceConfigWarning}
        <Notice type="warning" title="Arr Service Configuration">
          One or more rules reference a Radarr or Sonarr service from the
          original instance. Those IDs may not match your setup - review rule
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
      <AlertDialog.Title class="text-xl font-semibold text-foreground mb-2">
        Delete Rule
      </AlertDialog.Title>
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
