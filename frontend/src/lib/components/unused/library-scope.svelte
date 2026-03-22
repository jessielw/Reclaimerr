<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api, put_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import Save from "@lucide/svelte/icons/save";
  import Clapperboard from "@lucide/svelte/icons/clapperboard";
  import Tv from "@lucide/svelte/icons/tv";
  import Notice from "$lib/components/notice.svelte";
  import { toast } from "svelte-sonner";
  import { MediaType, SettingsTab, type LibraryType } from "$lib/types/shared";

  interface Props {
    libraries?: LibraryType[];
  }

  let { libraries = $bindable([]) }: Props = $props();

  let loading = $state(false);
  let syncing = $state(false);
  let saving = $state(false);

  // local copy for editing before saving
  let localLibraries = $state<LibraryType[]>([]);

  type SyncLibrariesResponse = {
    libraries: Array<{ id: string; name: string; type: MediaType }>;
    affected_rules: Array<{
      id: number;
      name: string;
      removed_library_ids: string[];
      remaining_library_ids: string[];
    }>;
  };

  // derive the main server from loaded libraries (or null if none)
  let mainServerType = $derived<SettingsTab.Jellyfin | SettingsTab.Plex | null>(
    localLibraries.length > 0
      ? (localLibraries[0].serviceType as
          | SettingsTab.Jellyfin
          | SettingsTab.Plex)
      : null,
  );

  async function loadLibraries() {
    try {
      loading = true;
      const services = await get_api<any>("/api/settings/services");
      const result: LibraryType[] = [];

      if (services.jellyfin?.libraries && services.jellyfin?.is_main) {
        services.jellyfin.libraries.forEach((lib: any) => {
          result.push({
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
      if (services.plex?.libraries && services.plex?.is_main) {
        services.plex.libraries.forEach((lib: any) => {
          result.push({
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

      localLibraries = result.sort((a, b) =>
        a.libraryName.localeCompare(b.libraryName),
      );
      libraries = [...localLibraries];
    } catch (err: any) {
      toast.error(`Error loading libraries: ${err.message}`);
    } finally {
      loading = false;
    }
  }

  async function syncLibraries() {
    syncing = true;
    try {
      // detect main server type from existing config
      const services = await get_api<any>("/api/settings/services");
      const mainServiceType = services.jellyfin?.is_main
        ? SettingsTab.Jellyfin
        : services.plex?.is_main
          ? SettingsTab.Plex
          : null;

      if (!mainServiceType) {
        toast.warning("No main media server configured.");
        return;
      }

      const response = await post_api<SyncLibrariesResponse>(
        "/api/settings/sync/libraries",
        {
          service_type: mainServiceType,
        },
      );
      await loadLibraries();

      if (localLibraries.length === 0) {
        toast.warning(
          "No libraries found. Make sure your main server is configured correctly.",
        );
      } else {
        toast.success(`Synced ${localLibraries.length} libraries.`);
      }

      if (response.affected_rules.length > 0) {
        toast.warning(
          `Library sync updated ${response.affected_rules.length} rule(s) because some libraries no longer exist.`,
          { duration: 8000 },
        );
      }
    } catch (err: any) {
      toast.error(`Error syncing libraries: ${err.message}`);
    } finally {
      syncing = false;
    }
  }

  async function saveSelections() {
    saving = true;
    try {
      await put_api(
        "/api/settings/libraries",
        localLibraries.map((lib) => ({ id: lib.id, selected: lib.selected })),
      );
      libraries = [...localLibraries];
      toast.success("Library scope saved.");
    } catch (err: any) {
      toast.error(`Error saving library scope: ${err.message}`);
    } finally {
      saving = false;
    }
  }

  onMount(() => {
    loadLibraries();
  });
</script>

<div class="rounded-lg border border-border p-5 space-y-4">
  <div class="flex items-start justify-between gap-4">
    <div>
      <h2 class="text-lg font-semibold text-foreground">Library Scope</h2>
      <p class="text-sm text-muted-foreground mt-0.5">
        Select which libraries to include when evaluating cleanup rules.
      </p>
    </div>
    <Button
      size="sm"
      class="cursor-pointer shrink-0 gap-2"
      disabled={syncing || saving}
      onclick={syncLibraries}
    >
      {#if syncing}
        <Spinner class="size-4" />
      {:else}
        <RefreshCw class="size-4" />
      {/if}
      Sync
    </Button>
  </div>

  {#if loading}
    <div class="flex items-center gap-2 text-muted-foreground text-sm py-2">
      <Spinner class="size-4" />
      Loading libraries...
    </div>
  {:else if localLibraries.length > 0}
    <div class="flex flex-wrap gap-1">
      {#each localLibraries as library}
        <Badge
          variant="secondary"
          class="text-sm px-3 py-1 rounded-full bg-muted text-muted-foreground m-0.5 w-55 justify-between
            {library.mediaType === MediaType.Movie
            ? 'border-movie'
            : 'border-series'}"
        >
          <div class="inline-flex flex-1 max-w-4/5 items-center gap-2">
            {#if library.mediaType === MediaType.Movie}
              <Clapperboard size="18" />
            {:else}
              <Tv size="18" />
            {/if}
            <span class="truncate" title={library.libraryName}>
              {library.libraryName}
            </span>
          </div>
          <Switch
            class="ml-1 cursor-pointer"
            checked={library.selected}
            onCheckedChange={(checked) => {
              library.selected = checked;
            }}
          />
        </Badge>
      {/each}
    </div>
    <div class="flex justify-end pt-2 border-t border-border">
      <Button
        size="sm"
        class="cursor-pointer gap-2"
        disabled={saving || syncing}
        onclick={saveSelections}
      >
        {#if saving}
          <Spinner class="size-4" />
          Saving...
        {:else}
          <Save class="size-4" />
          Save
        {/if}
      </Button>
    </div>
  {:else}
    <Notice title="No Libraries Loaded">
      <p>
        No libraries found. Click <strong>Sync</strong> to load libraries from your
        main server.
      </p>
    </Notice>
  {/if}
</div>
