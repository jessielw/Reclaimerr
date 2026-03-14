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
  import Clapperboard from "@lucide/svelte/icons/clapperboard";
  import Tv from "@lucide/svelte/icons/tv";
  import { toast } from "svelte-sonner";
  import {
    MediaType,
    SettingsTab,
    type ReclaimRule,
    type LibraryType,
  } from "$lib/types/shared";
  import RuleForm from "$lib/components/settings/rules/rule-form.svelte";
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

  // state to trigger new rule info message after creating/updating a rule
  let newRule = $state(false);
  let newRuleTimer: ReturnType<typeof setTimeout> | null = null;
  const newRuleDisplayDuration = 10000; // 10 seconds

  $effect(() => {
    if (!newRule) {
      return;
    }

    toast.success(
      "New candidates will appear next time the Scan Cleanup Candidates task is run. If you want them " +
        "sooner, you can manually trigger the scan.",
      {
        duration: newRuleDisplayDuration,
      },
    );
  });

  // delete dialog states
  let showDeleteDialog = $state(false);
  let ruleToDelete = $state<ReclaimRule | null>(null);

  // additional states
  let isSynced = $state(true); // assume synced until we check

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
      // debounce newRule ui
      newRule = true;
      if (newRuleTimer) clearTimeout(newRuleTimer);
      newRuleTimer = setTimeout(() => {
        newRule = false;
        newRuleTimer = null;
      }, newRuleDisplayDuration);
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
    editingRule = rule;
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
    const conditions: string[] = [];

    if (rule.library_ids && rule.library_ids.length > 0) {
      // map library IDs to names
      const libraryNames = rule.library_ids
        .map(
          (id) =>
            availableLibraries.find((lib) => lib.libraryId === id)?.libraryName,
        )
        .filter((name): name is string => name !== undefined);

      if (libraryNames.length > 0) {
        conditions.push(`Libraries: ${libraryNames.join(", ")}`);
      }
    }

    if (rule.min_popularity !== null || rule.max_popularity !== null) {
      if (rule.min_popularity !== null && rule.max_popularity !== null) {
        conditions.push(
          `Popularity: ${rule.min_popularity}-${rule.max_popularity}`,
        );
      } else if (rule.min_popularity !== null) {
        conditions.push(`Popularity ≥ ${rule.min_popularity}`);
      } else {
        conditions.push(`Popularity ≤ ${rule.max_popularity}`);
      }
    }

    if (rule.min_vote_average !== null || rule.max_vote_average !== null) {
      if (rule.min_vote_average !== null && rule.max_vote_average !== null) {
        conditions.push(
          `Rating: ${rule.min_vote_average}-${rule.max_vote_average}`,
        );
      } else if (rule.min_vote_average !== null) {
        conditions.push(`Rating ≥ ${rule.min_vote_average}`);
      } else {
        conditions.push(`Rating ≤ ${rule.max_vote_average}`);
      }
    }

    if (rule.min_days_since_added !== null) {
      conditions.push(`Added ${rule.min_days_since_added}+ days ago`);
    }

    if (rule.max_days_since_last_watched !== null) {
      conditions.push(
        `Not watched in ${rule.max_days_since_last_watched}+ days`,
      );
    }

    if (rule.include_never_watched) {
      conditions.push("Never watched");
    }

    if (rule.min_view_count !== null || rule.max_view_count !== null) {
      if (rule.min_view_count !== null && rule.max_view_count !== null) {
        conditions.push(`Views: ${rule.min_view_count}-${rule.max_view_count}`);
      } else if (rule.min_view_count !== null) {
        conditions.push(`Views ≥ ${rule.min_view_count}`);
      } else {
        conditions.push(`Views ≤ ${rule.max_view_count}`);
      }
    }

    return conditions.length > 0 ? conditions.join(" • ") : "No conditions set";
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
  <div class="p-8 text-center text-muted-foreground">
    <Spinner size="lg" class="text-primary" />
    <p class="mt-4">Loading rules...</p>
  </div>
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

      <!-- add new rule button -->
      <Button size="sm" onclick={createNewRule} class="cursor-pointer gap-2">
        <Plus class="size-4" />
        New Rule
      </Button>
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
          <Spinner class="size-5" />
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
                        <span class="capitalize">{rule.media_type}</span>
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

<!-- rule form modal -->
{#if showRuleForm}
  <RuleForm
    rule={editingRule}
    libraries={availableLibraries}
    onSave={handleSaveRule}
    onCancel={closeRuleForm}
  />
{/if}

<!-- confirm delete alert dialog -->
<AlertDialog.Root
  open={showDeleteDialog}
  onOpenChange={(v) => (showDeleteDialog = v)}
>
  <AlertDialog.Content
    class="bg-card border border-border rounded-lg p-6 max-w-md w-full"
  >
    <AlertDialog.Header>
      <AlertDialog.Title class="text-xl font-semibold text-foreground mb-2"
        >Delete User</AlertDialog.Title
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
