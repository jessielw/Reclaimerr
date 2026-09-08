<script lang="ts">
  import { onMount } from "svelte";
  import type { Component } from "svelte";
  import { Label } from "$lib/components/ui/label/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { delete_api, get_api, post_api, put_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import { Button } from "$lib/components/ui/button/index.js";
  import Save from "@lucide/svelte/icons/save";
  import Plus from "@lucide/svelte/icons/plus";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import PowerOff from "@lucide/svelte/icons/power-off";
  import ImageUp from "@lucide/svelte/icons/image-up";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import { Checkbox } from "$lib/components/ui/checkbox/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import {
    PageAccess,
    type GeneralSettings,
    type LeavingSoonCollectionSort,
    type PathMapping,
    type RequesterWatchUserMapping,
  } from "$lib/types/shared";
  import Notice from "$lib/components/notice.svelte";
  import { auth } from "$lib/stores/auth";
  import {
    DEFAULT_NEW_USER_ALLOWED_PAGES,
    PAGE_ACCESS_OPTIONS,
  } from "$lib/page-access";

  // Reclaimerr manages exactly these two collections per media server. Keep in
  // sync with backend/utils/helpers.py.
  const DEFAULT_LEAVING_SOON_MOVIE_TITLE = "Leaving Soon [Movies]";
  const DEFAULT_LEAVING_SOON_SERIES_TITLE = "Leaving Soon [Series]";
  const LEAVING_SOON_SORT_LABELS: Record<LeavingSoonCollectionSort, string> = {
    default: "Server default (don't change it)",
    alpha: "Alphabetical",
    leaving_soonest: "Leaving soonest first",
  };

  // props
  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  // state
  let loading = $state(true);
  let savingSettings = $state(false);
  let shuttingDown = $state(false);
  let shutdownDone = $state(false);
  const isAdmin = $derived($auth.user?.role === "admin");
  let generalSettings = $state({
    workerPollMinSeconds: "",
    workerPollMaxSeconds: "",
  });
  type PathMappingScope = {
    id: number;
    service_type: string;
    name: string;
    enabled: boolean;
  };

  let pathMappings = $state<PathMapping[]>([]);
  let moveDestinationMovies = $state("");
  let moveDestinationSeries = $state("");
  let mediaServerFallbackEnabled = $state(true);
  let addArrImportExclusionsOnDelete = $state(true);
  let autoDeleteMovieDelayDays = $state(14);
  let autoDeleteSeriesDelayDays = $state(7);
  let applicationUrl = $state("");
  let playbackMovieMinSeconds = $state(15);
  let playbackEpisodeMinSeconds = $state(7);
  let favoritesIgnoreEnabled = $state(false);
  let favoritesProtectAllUsers = $state(false);
  let favoritesUsernamesInput = $state("");
  let requesterWatchUserMappings = $state<RequesterWatchUserMapping[]>([]);
  // Edited on the User Signals page; carried here so saving does not reset it.
  let requesterWatchIgnoreRequestDate = $state(false);
  let defaultAllowedPages = $state<PageAccess[]>([
    ...DEFAULT_NEW_USER_ALLOWED_PAGES,
  ]);
  let leavingSoonEnabled = $state(false);
  let leavingSoonMovieCollectionTitle = $state(
    DEFAULT_LEAVING_SOON_MOVIE_TITLE,
  );
  let leavingSoonSeriesCollectionTitle = $state(
    DEFAULT_LEAVING_SOON_SERIES_TITLE,
  );
  let leavingSoonCollectionSort = $state<LeavingSoonCollectionSort>("default");
  // posters are saved the moment they are picked, unlike everything else on
  // this page, so they are keyed by kind rather than folded into the payload
  type PosterKind = "movies" | "series";
  let leavingSoonPosters = $state<Record<PosterKind, string | null>>({
    movies: null,
    series: null,
  });
  let posterBusy = $state<Record<PosterKind, boolean>>({
    movies: false,
    series: false,
  });
  let posterInputs: Record<PosterKind, HTMLInputElement | null> = {
    movies: null,
    series: null,
  };
  const leavingSoonMovieTitle = $derived(
    leavingSoonMovieCollectionTitle.trim() || DEFAULT_LEAVING_SOON_MOVIE_TITLE,
  );
  const leavingSoonSeriesTitle = $derived(
    leavingSoonSeriesCollectionTitle.trim() ||
      DEFAULT_LEAVING_SOON_SERIES_TITLE,
  );
  const leavingSoonTitlesCollide = $derived(
    leavingSoonEnabled &&
      leavingSoonMovieTitle.toLowerCase() ===
        leavingSoonSeriesTitle.toLowerCase(),
  );
  let defaultArrDeleteBehavior = $state<
    "unmonitor" | "unmonitor_only" | "remove_if_empty"
  >("unmonitor");
  let pathSuggestions = $state<string[]>([]);
  let pathMappingScopes = $state<PathMappingScope[]>([]);
  const serviceTypeOptions = ["plex", "jellyfin", "emby", "radarr", "sonarr"];

  const parseOptionalSeconds = (value: string): number | null => {
    if (!value.trim()) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };

  const parseFavoritesUsernames = (value: string): string[] => {
    const seen = new Set<string>();
    const usernames: string[] = [];
    for (const raw of value.split(/[\n,]+/)) {
      const normalized = raw.trim().toLowerCase();
      if (!normalized || seen.has(normalized)) continue;
      seen.add(normalized);
      usernames.push(normalized);
    }
    return usernames;
  };

  const toggleDefaultAllowedPage = (page: PageAccess, checked: boolean) => {
    if (checked) {
      defaultAllowedPages = [...new Set([...defaultAllowedPages, page])];
      return;
    }
    defaultAllowedPages = defaultAllowedPages.filter((item) => item !== page);
  };

  // save settings
  const saveSettings = async () => {
    if (leavingSoonTitlesCollide) {
      toast.error(
        "Leaving Soon movie and series collections must have different names",
      );
      return;
    }
    savingSettings = true;
    try {
      // validate input before saving
      const validationError = validateWorkerPoll();
      if (validationError) throw new Error(validationError);
      if (defaultAllowedPages.length === 0) {
        throw new Error("Select at least one default page for new users");
      }
      if (
        !Number.isInteger(autoDeleteMovieDelayDays) ||
        autoDeleteMovieDelayDays < 0 ||
        autoDeleteMovieDelayDays > 3650 ||
        !Number.isInteger(autoDeleteSeriesDelayDays) ||
        autoDeleteSeriesDelayDays < 0 ||
        autoDeleteSeriesDelayDays > 3650
      ) {
        throw new Error(
          "Automatic delete delays must be whole numbers from 0 to 3650",
        );
      }
      if (
        !Number.isInteger(playbackMovieMinSeconds) ||
        playbackMovieMinSeconds < 0 ||
        playbackMovieMinSeconds > 3600 ||
        !Number.isInteger(playbackEpisodeMinSeconds) ||
        playbackEpisodeMinSeconds < 0 ||
        playbackEpisodeMinSeconds > 3600
      ) {
        throw new Error(
          "Minimum playback durations must be whole numbers from 0 to 3600 seconds",
        );
      }

      // save settings to backend
      await put_api("/api/settings/general", {
        worker_poll_min_seconds: parseOptionalSeconds(
          generalSettings.workerPollMinSeconds,
        ),
        worker_poll_max_seconds: parseOptionalSeconds(
          generalSettings.workerPollMaxSeconds,
        ),
        path_mappings: pathMappings.filter(
          (m) => m.source_prefix.trim() && m.local_prefix.trim(),
        ),
        move_destination_movies: moveDestinationMovies,
        move_destination_series: moveDestinationSeries,
        media_server_fallback_enabled: mediaServerFallbackEnabled,
        default_arr_delete_behavior: defaultArrDeleteBehavior,
        add_arr_import_exclusions_on_delete: addArrImportExclusionsOnDelete,
        auto_delete_movie_delay_days: autoDeleteMovieDelayDays,
        auto_delete_series_delay_days: autoDeleteSeriesDelayDays,
        application_url: applicationUrl.trim() || null,
        playback_movie_min_seconds: playbackMovieMinSeconds,
        playback_episode_min_seconds: playbackEpisodeMinSeconds,
        favorites_ignore_enabled: favoritesIgnoreEnabled,
        favorites_protect_all_users: favoritesProtectAllUsers,
        favorites_usernames: parseFavoritesUsernames(favoritesUsernamesInput),
        requester_watch_user_mappings: requesterWatchUserMappings,
        requester_watch_ignore_request_date: requesterWatchIgnoreRequestDate,
        default_allowed_pages: defaultAllowedPages,
        leaving_soon_enabled: leavingSoonEnabled,
        leaving_soon_movie_collection_title: leavingSoonMovieTitle,
        leaving_soon_series_collection_title: leavingSoonSeriesTitle,
        leaving_soon_collection_sort: leavingSoonCollectionSort,
      });
      toast.success("General settings saved");
    } catch (error) {
      console.error("Error saving general settings:", error);
      toast.error(
        `Failed to save general settings: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      savingSettings = false;
    }
  };

  const validateWorkerPoll = (): string | void => {
    const workerPollMinSeconds = parseOptionalSeconds(
      generalSettings.workerPollMinSeconds,
    );
    const workerPollMaxSeconds = parseOptionalSeconds(
      generalSettings.workerPollMaxSeconds,
    );

    if (
      generalSettings.workerPollMinSeconds.trim() &&
      workerPollMinSeconds === null
    ) {
      return "Worker poll minimum must be a valid number of seconds";
    }

    if (
      generalSettings.workerPollMaxSeconds.trim() &&
      workerPollMaxSeconds === null
    ) {
      return "Worker poll maximum must be a valid number of seconds";
    }

    if (workerPollMinSeconds !== null && workerPollMinSeconds <= 0) {
      return "Worker poll minimum must be greater than 0 seconds";
    }

    if (workerPollMinSeconds !== null && workerPollMinSeconds > 60) {
      return "Worker poll minimum cannot exceed 60 seconds";
    }

    if (workerPollMaxSeconds !== null && workerPollMaxSeconds <= 0) {
      return "Worker poll maximum must be greater than 0 seconds";
    }

    if (workerPollMaxSeconds !== null && workerPollMaxSeconds > 60) {
      return "Worker poll maximum cannot exceed 60 seconds";
    }

    if (
      workerPollMinSeconds !== null &&
      workerPollMaxSeconds !== null &&
      workerPollMinSeconds > workerPollMaxSeconds
    ) {
      return "Worker poll minimum cannot be greater than worker poll maximum";
    }
  };

  const addPathMapping = () => {
    pathMappings = [
      ...pathMappings,
      {
        source_prefix: "",
        local_prefix: "",
        service_type: null,
        service_config_id: null,
      },
    ];
  };

  const removePathMapping = (index: number) => {
    pathMappings = pathMappings.filter((_, i) => i !== index);
  };

  const mappingScopeValue = (mapping: PathMapping) => {
    if (mapping.service_config_id != null) {
      return `config:${mapping.service_config_id}`;
    }
    if (mapping.service_type) return `type:${mapping.service_type}`;
    return "global";
  };

  const setMappingScope = (mapping: PathMapping, value: string) => {
    if (value === "global") {
      mapping.service_type = null;
      mapping.service_config_id = null;
      return;
    }
    if (value.startsWith("type:")) {
      mapping.service_type = value.slice("type:".length);
      mapping.service_config_id = null;
      return;
    }
    if (value.startsWith("config:")) {
      const id = Number(value.slice("config:".length));
      const scope = pathMappingScopes.find((s) => s.id === id);
      mapping.service_type = scope?.service_type ?? null;
      mapping.service_config_id = Number.isFinite(id) ? id : null;
    }
  };

  // shutdown app (desktop mode ONLY and requires admin)
  const POSTER_LABELS: Record<PosterKind, string> = {
    movies: "Movie",
    series: "Series",
  };

  const posterUrl = (kind: PosterKind): string | null => {
    const filename = leavingSoonPosters[kind];
    return filename ? `/static/collection-posters/${filename}` : null;
  };

  const uploadPoster = async (kind: PosterKind, event: Event) => {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    // clear the input so re-picking the same file still fires a change event
    input.value = "";
    if (!file) return;

    posterBusy = { ...posterBusy, [kind]: true };
    try {
      const formData = new FormData();
      formData.append("poster", file);
      const response: { message: string; path: string } = await post_api(
        `/api/settings/general/leaving-soon-poster/${kind}`,
        formData,
      );
      leavingSoonPosters = { ...leavingSoonPosters, [kind]: response.path };
      toast.success(`${POSTER_LABELS[kind]} collection poster uploaded`);
    } catch (error: any) {
      toast.error(`Failed to upload poster: ${error.message}`);
    } finally {
      posterBusy = { ...posterBusy, [kind]: false };
    }
  };

  const removePoster = async (kind: PosterKind) => {
    posterBusy = { ...posterBusy, [kind]: true };
    try {
      await delete_api(`/api/settings/general/leaving-soon-poster/${kind}`);
      leavingSoonPosters = { ...leavingSoonPosters, [kind]: null };
      toast.success(`${POSTER_LABELS[kind]} collection poster removed`);
    } catch (error: any) {
      toast.error(`Failed to remove poster: ${error.message}`);
    } finally {
      posterBusy = { ...posterBusy, [kind]: false };
    }
  };

  const shutdownApp = async () => {
    if (shuttingDown || shutdownDone) return;
    shuttingDown = true;
    try {
      await post_api("/api/system/shutdown", {});
      shutdownDone = true;
      toast.success("Shutting down… you can close this tab.");
    } catch (error: unknown) {
      const status =
        error && typeof error === "object" && "status" in error
          ? (error as { status: number }).status
          : null;
      if (status === 503) {
        toast.info("Shutdown is not available in server mode.");
      } else {
        toast.error("Failed to shut down the application.");
      }
    } finally {
      shuttingDown = false;
    }
  };

  onMount(async () => {
    try {
      const settings: GeneralSettings = await get_api("/api/settings/general");
      if (settings) {
        generalSettings = {
          workerPollMinSeconds:
            settings.worker_poll_min_seconds?.toString() ?? "",
          workerPollMaxSeconds:
            settings.worker_poll_max_seconds?.toString() ?? "",
        };
        pathMappings = settings.path_mappings ?? [];
        moveDestinationMovies = settings.move_destination_movies ?? "";
        moveDestinationSeries = settings.move_destination_series ?? "";
        mediaServerFallbackEnabled =
          settings.media_server_fallback_enabled ?? true;
        defaultArrDeleteBehavior =
          settings.default_arr_delete_behavior ?? "unmonitor";
        addArrImportExclusionsOnDelete =
          settings.add_arr_import_exclusions_on_delete ?? true;
        autoDeleteMovieDelayDays = settings.auto_delete_movie_delay_days ?? 14;
        autoDeleteSeriesDelayDays = settings.auto_delete_series_delay_days ?? 7;
        applicationUrl = settings.application_url ?? "";
        playbackMovieMinSeconds = settings.playback_movie_min_seconds ?? 15;
        playbackEpisodeMinSeconds = settings.playback_episode_min_seconds ?? 7;
        favoritesIgnoreEnabled = settings.favorites_ignore_enabled ?? false;
        favoritesProtectAllUsers =
          settings.favorites_protect_all_users ?? false;
        favoritesUsernamesInput = (settings.favorites_usernames ?? []).join(
          ", ",
        );
        requesterWatchUserMappings =
          settings.requester_watch_user_mappings ?? [];
        requesterWatchIgnoreRequestDate =
          settings.requester_watch_ignore_request_date ?? false;
        defaultAllowedPages =
          settings.default_allowed_pages?.length > 0
            ? settings.default_allowed_pages
            : [...DEFAULT_NEW_USER_ALLOWED_PAGES];
        leavingSoonEnabled = settings.leaving_soon_enabled ?? false;
        leavingSoonMovieCollectionTitle =
          settings.leaving_soon_movie_collection_title ??
          DEFAULT_LEAVING_SOON_MOVIE_TITLE;
        leavingSoonSeriesCollectionTitle =
          settings.leaving_soon_series_collection_title ??
          DEFAULT_LEAVING_SOON_SERIES_TITLE;
        leavingSoonCollectionSort =
          settings.leaving_soon_collection_sort ?? "default";
        leavingSoonPosters = {
          movies: settings.leaving_soon_movie_poster_path ?? null,
          series: settings.leaving_soon_series_poster_path ?? null,
        };
      }
    } catch (error) {
      console.error("Error fetching general settings:", error);
      toast.error("Failed to load general settings");
    } finally {
      loading = false;
    }

    // fetch path suggestions separately (not really critical and we'll ignore errors)
    try {
      const suggestions = await get_api<string[]>(
        "/api/settings/general/path-suggestions",
      );
      pathSuggestions = suggestions ?? [];
    } catch {
      // ignore all errors
    }

    try {
      const scopes = await get_api<PathMappingScope[]>(
        "/api/settings/general/path-mapping-scopes",
      );
      pathMappingScopes = scopes ?? [];
    } catch {
      // ignore all errors
    }
  });
</script>

<div class="space-y-6">
  <!-- header -->
  <div>
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {#if svgIcon}
        {@const Icon = svgIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      General
    </h2>
    <p class="text-sm text-muted-foreground mt-1">Manage general settings</p>
  </div>

  <!-- check if loading -->
  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner class="w-12 h-12 text-primary" />
    </div>
  {:else}
    <!-- worker polling -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm mt-6">
      <h3 class="font-semibold text-foreground items-center">Worker polling</h3>
      <p class="text-muted-foreground text-sm mb-3">
        Configure the background worker's polling behavior when idle. Adjusting
        these settings can help balance prompt job processing with resource
        usage. Recommended values are between 0.5 and 5 seconds (max 60
        seconds).
      </p>

      <div class="grid gap-4 md:grid-cols-2">
        <!-- minimum poll seconds -->
        <div>
          <Label for="workerPollMinSeconds" class="mb-2">
            <span class="text-sm text-foreground">Minimum Poll Seconds</span>
          </Label>
          <Input
            id="workerPollMinSeconds"
            name="workerPollMinSeconds"
            type="number"
            min="0.1"
            max="60"
            step="0.1"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            placeholder="Default: 0.5"
            bind:value={generalSettings.workerPollMinSeconds}
          />
        </div>

        <!-- maximum poll seconds -->
        <div>
          <Label for="workerPollMaxSeconds" class="mb-2">
            <span class="text-sm text-foreground">Maximum Poll Seconds</span>
          </Label>
          <Input
            id="workerPollMaxSeconds"
            name="workerPollMaxSeconds"
            type="number"
            min="0.1"
            max="60"
            step="0.1"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            placeholder="Default: 5"
            bind:value={generalSettings.workerPollMaxSeconds}
          />
        </div>
      </div>
    </div>

    <!-- path mappings -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <h3 class="font-semibold text-foreground">Path Mappings</h3>
      <p class="text-muted-foreground text-sm mb-3">
        Map media server paths to local filesystem paths. Required when
        Reclaimerr runs on the host but your media server reports
        Docker/container paths (e.g. <code class="font-mono text-xs"
          >/movies</code
        >
        →
        <code class="font-mono text-xs">/mnt/data/movies</code>). Leave empty if
        paths are directly accessible. Scope mappings when multiple services
        report similar container paths differently.
      </p>

      <div class="space-y-2">
        <!-- path mappings -->
        {#each pathMappings as mapping, i}
          <div
            class="grid gap-2 items-center md:grid-cols-[1fr_1fr_12rem_auto]"
          >
            <!-- media server prefix -->
            <div>
              <Label class="mb-2 text-xs">Media server path prefix</Label>
              <Input
                type="text"
                list="path-suggestions"
                class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono text-sm"
                placeholder="/movies"
                bind:value={mapping.source_prefix}
              />
            </div>

            <!-- local prefix -->
            <div>
              <Label class="mb-2 text-xs">Local path prefix</Label>
              <Input
                type="text"
                class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono text-sm"
                placeholder="/mnt/data/movies"
                bind:value={mapping.local_prefix}
              />
            </div>

            <!-- scope -->
            <div>
              <Label class="mb-2 text-xs">Scope</Label>
              <div class="inline-flex items-center gap-2 w-full">
                <Select.Root
                  type="single"
                  value={mappingScopeValue(mapping)}
                  onValueChange={(value) => setMappingScope(mapping, value)}
                >
                  <Select.Trigger class="w-full cursor-pointer text-foreground">
                    {#if mappingScopeValue(mapping) === "global"}
                      Global
                    {:else if mappingScopeValue(mapping).startsWith("type:")}
                      Any {mappingScopeValue(mapping).slice("type:".length)}
                    {:else if pathMappingScopes.find((s) => `config:${s.id}` === mappingScopeValue(mapping))}
                      {@const scope = pathMappingScopes.find(
                        (s) => `config:${s.id}` === mappingScopeValue(mapping),
                      )}
                      {scope?.name} ({scope?.service_type})
                    {/if}
                  </Select.Trigger>
                  <Select.Content>
                    <Select.Item value="global" class="cursor-pointer"
                      >Global</Select.Item
                    >
                    {#if serviceTypeOptions.length > 0}
                      <Select.Separator />
                      {#each serviceTypeOptions as serviceType}
                        <Select.Item
                          value={`type:${serviceType}`}
                          class="cursor-pointer">Any {serviceType}</Select.Item
                        >
                      {/each}
                    {/if}
                    {#if pathMappingScopes.length > 0}
                      <Select.Separator />
                      <Select.Label>Configured services</Select.Label>
                      {#each pathMappingScopes as scope}
                        <Select.Item
                          value={`config:${scope.id}`}
                          class="cursor-pointer"
                          >{scope.name} ({scope.service_type})</Select.Item
                        >
                      {/each}
                    {/if}
                  </Select.Content>
                </Select.Root>

                <!-- remove mapping button -->
                <Button
                  size="icon-sm"
                  class="bg-destructive/70 hover:bg-destructive/80 cursor-pointer shrink-0"
                  onclick={() => removePathMapping(i)}
                  aria-label="Remove mapping"
                >
                  <Trash2 class="size-4" />
                </Button>
              </div>
            </div>
          </div>
        {/each}

        <!-- path suggestions -->
        {#if pathSuggestions.length > 0}
          <datalist id="path-suggestions">
            {#each pathSuggestions as suggestion}
              <option value={suggestion}></option>
            {/each}
          </datalist>
          <div class="flex flex-wrap items-center gap-1.5 pt-1">
            <span class="text-xs text-muted-foreground shrink-0">Detected:</span
            >
            <!-- suggestion button chips -->
            {#each pathSuggestions as suggestion}
              <button
                type="button"
                class="cursor-pointer rounded border border-border bg-muted/40 px-1.5 py-0.5
                  font-mono text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
                onclick={() => {
                  const empty = pathMappings.findIndex(
                    (m) => !m.source_prefix.trim(),
                  );
                  if (empty !== -1) {
                    pathMappings[empty].source_prefix = suggestion;
                  } else {
                    pathMappings = [
                      ...pathMappings,
                      {
                        source_prefix: suggestion,
                        local_prefix: "",
                        service_type: null,
                        service_config_id: null,
                      },
                    ];
                  }
                }}>{suggestion}</button
              >
            {/each}
          </div>
        {/if}
      </div>

      <!-- add mapping button -->
      <Button
        size="sm"
        class="mt-3 cursor-pointer gap-2"
        onclick={addPathMapping}
      >
        <Plus class="size-4" />
        Add mapping
      </Button>
    </div>

    <!-- public application URL -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <h3 class="font-semibold text-foreground mb-1">Public Application URL</h3>
      <p class="text-muted-foreground text-sm mb-3">
        Set the public URL users use to reach Reclaimerr through your reverse
        proxy. Reclaimerr uses it when building Plex and OIDC callback URLs.
        Leave it blank to use the current request URL instead.
      </p>
      <div class="max-w-xl">
        <Label for="applicationUrl" class="mb-2">
          <span class="text-sm text-foreground">Application URL</span>
        </Label>
        <Input
          id="applicationUrl"
          name="applicationUrl"
          type="url"
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
          placeholder="https://app.example.com"
          bind:value={applicationUrl}
        />
      </div>
    </div>

    <!-- leaving soon collections -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <div class="flex items-center justify-between mb-1">
        <h3 class="font-semibold text-foreground">Leaving Soon Collections</h3>
        <Switch id="leavingSoonEnabled" bind:checked={leavingSoonEnabled} />
      </div>
      <p class="text-muted-foreground text-sm mb-3">
        Keep an auto-synced "Leaving Soon" row on enabled Plex, Jellyfin, and
        Emby servers. Reclaimerr updates these collections after each candidate
        scan and removes stale entries automatically.
      </p>

      {#if leavingSoonEnabled}
        <div class="grid gap-4 sm:grid-cols-2 max-w-2xl">
          <div>
            <Label for="leavingSoonMovieCollectionTitle" class="mb-2">
              <span class="text-sm text-foreground">Movie Collection Name</span>
            </Label>
            <Input
              id="leavingSoonMovieCollectionTitle"
              name="leavingSoonMovieCollectionTitle"
              type="text"
              class="input-hover-el text-foreground placeholder:text-muted-foreground"
              placeholder={DEFAULT_LEAVING_SOON_MOVIE_TITLE}
              bind:value={leavingSoonMovieCollectionTitle}
              maxlength={100}
            />
          </div>
          <div>
            <Label for="leavingSoonSeriesCollectionTitle" class="mb-2">
              <span class="text-sm text-foreground">Series Collection Name</span
              >
            </Label>
            <Input
              id="leavingSoonSeriesCollectionTitle"
              name="leavingSoonSeriesCollectionTitle"
              type="text"
              class="input-hover-el text-foreground placeholder:text-muted-foreground"
              placeholder={DEFAULT_LEAVING_SOON_SERIES_TITLE}
              bind:value={leavingSoonSeriesCollectionTitle}
              maxlength={100}
            />
          </div>
        </div>
        <p class="text-xs text-muted-foreground mt-2 break-all">
          Reclaimerr manages exactly these two collections per server. Names are
          used verbatim - leave a field blank to fall back to
          <strong>{DEFAULT_LEAVING_SOON_MOVIE_TITLE}</strong>
          or <strong>{DEFAULT_LEAVING_SOON_SERIES_TITLE}</strong>.
        </p>
        {#if leavingSoonTitlesCollide}
          <p class="text-xs text-destructive mt-2">
            The movie and series collections must have different names.
          </p>
        {/if}
        <div class="mt-4 max-w-md">
          <Label for="leavingSoonCollectionSort" class="mb-2">
            <span class="text-sm text-foreground">Collection Sort (Plex)</span>
          </Label>
          <Select.Root
            type="single"
            bind:value={leavingSoonCollectionSort}
            name="leavingSoonCollectionSort"
          >
            <Select.Trigger
              id="leavingSoonCollectionSort"
              class="w-full cursor-pointer text-foreground"
            >
              {LEAVING_SOON_SORT_LABELS[leavingSoonCollectionSort]}
            </Select.Trigger>
            <Select.Content>
              <Select.Item value="default" class="cursor-pointer">
                {LEAVING_SOON_SORT_LABELS.default}
              </Select.Item>
              <Select.Item value="alpha" class="cursor-pointer">
                {LEAVING_SOON_SORT_LABELS.alpha}
              </Select.Item>
              <Select.Item value="leaving_soonest" class="cursor-pointer">
                {LEAVING_SOON_SORT_LABELS.leaving_soonest}
              </Select.Item>
            </Select.Content>
          </Select.Root>
          <p class="text-xs text-muted-foreground mt-2">
            <strong>Leaving soonest first</strong> orders the collection by each
            item's deletion deadline, so whatever disappears next sits at the
            front.
            <strong>Server default</strong> leaves the collection's own ordering untouched.
          </p>
        </div>

        <!-- custom collection posters -->
        <div class="mt-4">
          <span class="text-sm text-foreground font-medium"
            >Custom collection posters</span
          >
          <p class="text-xs text-muted-foreground mt-1">
            Upload your own cover art for the managed collections. Unlike the
            rest of this page, a poster is saved as soon as you pick it - there
            is nothing to Save.
          </p>
          <div class="grid gap-4 sm:grid-cols-2 max-w-2xl mt-3">
            {#each ["movies", "series"] as const as kind (kind)}
              <div class="flex gap-3">
                <div
                  class="w-24 shrink-0 aspect-2/3 rounded-md border bg-muted/50 overflow-hidden flex items-center justify-center"
                >
                  {#if posterUrl(kind)}
                    <img
                      src={posterUrl(kind)}
                      alt="{POSTER_LABELS[kind]} collection poster"
                      class="w-full h-full object-cover"
                    />
                  {:else}
                    <span
                      class="text-[0.65rem] text-muted-foreground text-center px-2"
                    >
                      No custom poster
                    </span>
                  {/if}
                </div>
                <div class="flex flex-col gap-2 justify-center">
                  <span class="text-sm text-foreground"
                    >{POSTER_LABELS[kind]} collection</span
                  >
                  <input
                    type="file"
                    class="hidden"
                    accept="image/jpeg,image/png,image/webp"
                    bind:this={posterInputs[kind]}
                    onchange={(event) => uploadPoster(kind, event)}
                  />
                  <Button
                    variant="secondary"
                    size="sm"
                    class="cursor-pointer"
                    disabled={posterBusy[kind]}
                    onclick={() => posterInputs[kind]?.click()}
                  >
                    {#if posterBusy[kind]}
                      <Spinner class="size-4" />
                    {:else}
                      <ImageUp class="size-4" />
                    {/if}
                    {leavingSoonPosters[kind]
                      ? "Replace poster"
                      : "Upload poster"}
                  </Button>
                  {#if leavingSoonPosters[kind]}
                    <Button
                      variant="ghost"
                      size="sm"
                      class="cursor-pointer text-destructive"
                      disabled={posterBusy[kind]}
                      onclick={() => removePoster(kind)}
                    >
                      <Trash2 class="size-4" />
                      Remove
                    </Button>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
          <p class="text-xs text-muted-foreground mt-3">
            JPEG, PNG, or WebP up to 5 MB. The image is re-encoded to JPEG and
            stored locally, then pushed to your enabled Plex, Jellyfin, and Emby
            servers. Reclaimerr re-applies it on <strong>every sync</strong>, so
            other tools that manage collection artwork (e.g. Kometa,
            Posterizarr) will be overwritten.
          </p>
          <p class="text-xs text-muted-foreground mt-2">
            Removing a poster only stops Reclaimerr pushing it. On Plex it
            disappears at the next sync, which rebuilds the collection; on
            Jellyfin and Emby the last poster pushed stays until you change it
            there.
          </p>
        </div>

        <Notice class="mt-2" type="info" title="Note">
          Plex stores collections <strong>per library</strong>, while Jellyfin
          and Emby use
          <strong>global</strong> collections. On Plex, each collection is split
          across libraries; on Jellyfin and Emby it appears as a single global
          collection.
          <br />
          <br />
          Renaming a collection here moves it: Reclaimerr deletes the collection under
          the old name on the next scan and rebuilds it under the new one.
          <br />
          <br />
          <strong
            >Do not rename or modify these collections on the media server -
            Reclaimerr depends on their names to manage them.</strong
          >
          <br />
          <br />
          Collection sort applies to <strong>Plex only</strong>. Jellyfin and
          Emby have no server-side ordering for a collection - each client
          decides how to sort it - so the setting is ignored there.
        </Notice>
      {/if}
    </div>

    <!-- move destination settings -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <h3 class="font-semibold text-foreground mb-1">
        Move Destination Folders
      </h3>
      <p class="text-muted-foreground text-sm mb-3">
        Configure where moved cleanup candidates are placed. Automatic
        move-instead-of-delete behavior is enabled per cleanup rule.
      </p>

      <div class="grid gap-4 md:grid-cols-2">
        <div>
          <Label for="moveDestinationMovies" class="mb-2">
            <span class="text-sm text-foreground">Movies Destination</span>
          </Label>
          <Input
            id="moveDestinationMovies"
            name="moveDestinationMovies"
            type="text"
            class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono"
            placeholder="/mnt/data/reclaimed/movies"
            bind:value={moveDestinationMovies}
          />
        </div>
        <div>
          <Label for="moveDestinationSeries" class="mb-2">
            <span class="text-sm text-foreground">Series Destination</span>
          </Label>
          <Input
            id="moveDestinationSeries"
            name="moveDestinationSeries"
            type="text"
            class="input-hover-el text-foreground placeholder:text-muted-foreground font-mono"
            placeholder="/mnt/data/reclaimed/series"
            bind:value={moveDestinationSeries}
          />
        </div>
      </div>
      <p class="text-xs text-muted-foreground mt-2">
        Local paths where reclaimed files will be placed. Folder structure is
        preserved under the matched path mapping root; without a mapping,
        Reclaimerr preserves the media folder. Manual move actions are available
        when the relevant destination is configured.
      </p>
    </div>

    <!-- media server fallback deletion -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <div class="flex items-center justify-between mb-1">
        <h3 class="font-semibold text-foreground">
          Allow Media Server Fallback Deletion
        </h3>
        <Switch
          id="mediaServerFallbackEnabled"
          bind:checked={mediaServerFallbackEnabled}
        />
      </div>
      <p class="text-muted-foreground text-sm">
        When Radarr/Sonarr cannot handle a deletion (e.g. a partial version
        deletion, or the item is not tracked in any arr instance), fall back to
        deleting via the media server (Jellyfin/Emby/Plex) directly. Disable
        this if your media server has read only file access.
      </p>
    </div>

    <!-- add Arr import list exclusions on delete -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <div class="flex items-center justify-between mb-1">
        <h3 class="font-semibold text-foreground">
          Add Arr Import List Exclusions on Delete
        </h3>
        <Switch
          id="addArrImportExclusionsOnDelete"
          bind:checked={addArrImportExclusionsOnDelete}
        />
      </div>
      <p class="text-muted-foreground text-sm">
        When enabled, delete actions sent to Radarr/Sonarr also add Arr import
        list exclusions to reduce automatic re-add/re-import behavior. Unmonitor
        only actions are not affected.
      </p>
    </div>

    <!-- default page access -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <h3 class="font-semibold text-foreground mb-1">
        Default Page Access for New Users
      </h3>
      <p class="text-muted-foreground text-sm mb-3">
        Applies to newly created and media-login provisioned non-admin users.
        Existing users keep their current page access until edited.
      </p>
      <div class="grid gap-2 sm:grid-cols-2">
        {#each PAGE_ACCESS_OPTIONS as option}
          <Label
            class="flex items-start gap-2 rounded-md border border-border bg-background/40 p-3 text-sm
              text-foreground cursor-pointer"
          >
            <Checkbox
              checked={defaultAllowedPages.includes(option.value)}
              onCheckedChange={(checked) =>
                toggleDefaultAllowedPage(option.value, checked === true)}
            />
            <span>
              <span class="font-medium">{option.label}</span>
              <span class="block text-xs text-muted-foreground">
                {option.description}
              </span>
            </span>
          </Label>
        {/each}
      </div>
      {#if defaultAllowedPages.length === 0}
        <p class="mt-2 text-xs text-destructive">
          Select at least one default page.
        </p>
      {/if}
    </div>

    <!-- automatic deletion defaults -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <div class="mb-1">
        <h3 class="font-semibold text-foreground">
          Default Auto-Delete Review Periods
        </h3>
      </div>
      <p class="text-muted-foreground text-sm mb-3">
        These defaults are used by cleanup rules that explicitly enable
        automatic deletion and do not set their own delay. The
        <code>Delete Cleanup Candidates</code> task must still be scheduled for eligible
        candidates to be deleted.
      </p>
      <div class="grid gap-3 sm:grid-cols-2 mb-3">
        <div class="space-y-2">
          <Label for="autoDeleteMovieDelayDays" class="text-sm text-foreground">
            Movie review period (days)
          </Label>
          <Input
            id="autoDeleteMovieDelayDays"
            type="number"
            min="0"
            max="3650"
            step="1"
            bind:value={autoDeleteMovieDelayDays}
          />
        </div>
        <div class="space-y-2">
          <Label
            for="autoDeleteSeriesDelayDays"
            class="text-sm text-foreground"
          >
            TV review period (days)
          </Label>
          <Input
            id="autoDeleteSeriesDelayDays"
            type="number"
            min="0"
            max="3650"
            step="1"
            bind:value={autoDeleteSeriesDelayDays}
          />
        </div>
      </div>
      <p class="text-xs text-muted-foreground mb-3">
        The countdown starts when an item first becomes a candidate. Rule-level
        overrides can replace these defaults; when multiple auto-delete-enabled
        rules match, the longest delay wins. Use 0 for immediate eligibility.
      </p>
      <div
        class="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-sm text-foreground"
      >
        <p class="font-medium">Rule opt-in required</p>
        <p class="mt-1">
          Reclaimerr only auto-deletes candidates created by cleanup rules with
          automatic deletion enabled. Protected media and items with pending
          protection or delete requests are skipped.
        </p>
      </div>
    </div>

    <!-- playback activity thresholds -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <div class="mb-1">
        <h3 class="font-semibold text-foreground">Minimum Playback Duration</h3>
      </div>
      <p class="text-muted-foreground text-sm mb-3">
        Playback events shorter than these durations are treated as accidental
        scrubs and are not recorded as activity, so they never count toward
        playback rule conditions. This applies to history imported from now on;
        history already imported keeps the events it was imported with.
      </p>
      <div class="grid gap-3 sm:grid-cols-2">
        <div class="space-y-2">
          <Label for="playbackMovieMinSeconds" class="text-sm text-foreground">
            Movie minimum (seconds)
          </Label>
          <Input
            id="playbackMovieMinSeconds"
            type="number"
            min="0"
            max="3600"
            step="1"
            bind:value={playbackMovieMinSeconds}
          />
        </div>
        <div class="space-y-2">
          <Label
            for="playbackEpisodeMinSeconds"
            class="text-sm text-foreground"
          >
            Episode minimum (seconds)
          </Label>
          <Input
            id="playbackEpisodeMinSeconds"
            type="number"
            min="0"
            max="3600"
            step="1"
            bind:value={playbackEpisodeMinSeconds}
          />
        </div>
      </div>
    </div>

    <!-- default ARR delete behavior -->
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
      <h3 class="font-semibold text-foreground mb-1">
        Default ARR Delete Behavior
      </h3>
      <p class="text-muted-foreground text-sm mb-3">
        Fallback behavior for deletes that are not tied to a matched cleanup
        rule, such as approved delete requests. Rule-level ARR actions still
        override this.
      </p>
      <div class="max-w-md">
        <Label for="defaultArrDeleteBehavior" class="mb-2">
          <span class="text-sm text-foreground">Fallback ARR Action</span>
        </Label>
        <Select.Root
          type="single"
          bind:value={defaultArrDeleteBehavior}
          name="defaultArrDeleteBehavior"
        >
          <Select.Trigger
            id="defaultArrDeleteBehavior"
            class="w-full cursor-pointer text-foreground"
          >
            {defaultArrDeleteBehavior === "remove_if_empty"
              ? "Remove from ARR when empty"
              : defaultArrDeleteBehavior === "unmonitor_only"
                ? "Unmonitor only (keep files)"
                : "Unmonitor after deletion"}
          </Select.Trigger>
          <Select.Content>
            <Select.Item value="unmonitor" class="cursor-pointer">
              Unmonitor after deletion
            </Select.Item>
            <Select.Item value="unmonitor_only" class="cursor-pointer">
              Unmonitor only (keep files)
            </Select.Item>
            <Select.Item value="remove_if_empty" class="cursor-pointer">
              Remove from ARR when empty
            </Select.Item>
          </Select.Content>
        </Select.Root>
      </div>
      <p class="text-xs text-muted-foreground mt-2">
        <code>Unmonitor</code> keeps the ARR entry but prevents re-grabs, and
        still deletes the files. <code>Unmonitor only</code> unmonitors the ARR
        entry and leaves the files on disk untouched for manual review.
        <code>Remove when empty</code> removes the ARR item only after the last remaining
        file for that movie or series is gone.
      </p>
    </div>

    <!-- shutdown (desktop mode / admin only) -->
    {#if isAdmin}
      <div
        class="bg-muted/50 border border-destructive/30 rounded-lg p-4 shadow-sm"
      >
        <h3 class="font-semibold text-foreground">Shutdown</h3>
        <p class="text-muted-foreground text-sm mb-3">
          Stop the Reclaimerr desktop process. Only available when running in
          desktop mode. Use the tray icon or this button to exit cleanly.
        </p>
        <Button
          variant="destructive"
          size="sm"
          class="cursor-pointer gap-2"
          disabled={shuttingDown || shutdownDone}
          onclick={shutdownApp}
        >
          {#if shuttingDown}
            <Spinner class="size-4" />
          {:else}
            <PowerOff class="size-4" />
          {/if}
          {shutdownDone ? "Shutting down…" : "Shutdown"}
        </Button>
      </div>
    {/if}

    <!-- save -->
    <div class="flex gap-3 justify-end">
      <Button
        onclick={saveSettings}
        disabled={savingSettings || leavingSoonTitlesCollide}
        class="cursor-pointer gap-2"
      >
        {#if savingSettings}
          <Spinner class="size-4" />
        {:else}
          <Save class="size-4" />
        {/if}
        Save
      </Button>
    </div>
  {/if}
</div>
