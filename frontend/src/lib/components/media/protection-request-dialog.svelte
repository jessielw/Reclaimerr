<script lang="ts">
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { get_api, post_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import { auth } from "$lib/stores/auth";
  import {
    type EpisodeWithStatus,
    MediaType,
    Permission,
    type ProtectionRequest,
    type SeasonWithStatus,
  } from "$lib/types/shared";
  import * as Select from "$lib/components/ui/select/index.js";
  import { Textarea } from "$lib/components/ui/textarea/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";

  const TMDB_POSTER_WIDTH = 342;
  const inputPlaceHolderText =
    "Explain why this should be kept (e.g., 'Planning to watch " +
    "soon', 'Personal favorite', etc.)";

  interface VersionLike {
    id: number;
    path: string | null;
    size: number;
    library_name: string;
    container: string | null;
  }

  interface MediaLike {
    id: number;
    versions?: VersionLike[];
    title: string;
    year: number | null;
    poster_url: string | null;
    status: { is_candidate: boolean };
  }

  interface Props {
    open: boolean;
    media: MediaLike | null;
    mediaType: MediaType;
    // pre-set a season (skips the scope picker and targets just this season
    seasonId?: number | null;
    // season number for display context when seasonId is set
    seasonNumber?: number | null;
    // pre-set an episode (skips the scope picker and targets just this episode)
    episodeId?: number | null;
    episodeNumber?: number | null;
    episodeName?: string | null;
    onClose?: () => void;
    onSuccess?: (request: ProtectionRequest) => void;
  }

  let {
    open = $bindable(),
    media,
    mediaType,
    seasonId = null,
    seasonNumber = null,
    episodeId = null,
    episodeNumber = null,
    episodeName = null,
    onClose,
    onSuccess,
  }: Props = $props();

  const isAdmin = $derived(
    $auth.user?.role === "admin" ||
      ($auth.user?.permissions ?? []).includes(Permission.AutoApprove),
  );

  // show the season checklist only for series without a pre-selected season
  const showScopePicker = $derived(
    mediaType === MediaType.Series && seasonId == null && episodeId == null,
  );

  // show version picker for movies with 2+ versions
  const showVersionPicker = $derived(
    mediaType === MediaType.Movie && (media?.versions?.length ?? 0) >= 2,
  );

  // form state
  let reason = $state("");
  let submitting = $state(false);
  let duration = $state("30");
  let customDays = $state("30");

  // scope state
  let seasons = $state<SeasonWithStatus[]>([]);
  let episodes = $state<EpisodeWithStatus[]>([]);
  let loadingSeasons = $state(false);
  let loadingEpisodes = $state(false);
  let scopeExpanded = $state(true);
  let versionPickerExpanded = $state(true);
  let scopeMode = $state<"series" | "seasons" | "episodes">("series");
  let selectedSeasonIds = $state<Set<number>>(new Set());
  let selectedEpisodeIds = $state<Set<number>>(new Set());

  // version selection: null = whole movie, number = specific version id
  let selectedVersionId = $state<number | null>(null);

  $effect(() => {
    if (open) {
      reason = isAdmin ? "Admin decision" : "";
      duration = isAdmin ? "permanent" : "30";
      customDays = "30";
      scopeMode = "series";
      selectedSeasonIds = new Set();
      selectedEpisodeIds = new Set();
      scopeExpanded = true;
      versionPickerExpanded = true;
      seasons = [];
      episodes = [];
      // auto-pick when there's exactly one version; otherwise default to whole-movie (null)
      const vers = media?.versions ?? [];
      selectedVersionId = vers.length === 1 ? vers[0].id : null;
      if (showScopePicker && media) {
        fetchSeasons(media.id);
        fetchEpisodes(media.id);
      }
    }
  });

  const fetchSeasons = async (seriesId: number) => {
    loadingSeasons = true;
    try {
      seasons = await get_api<SeasonWithStatus[]>(
        `/api/media/series/${seriesId}/seasons`,
      );
    } catch (err: any) {
      toast.error(`Failed to load seasons: ${err.message}`);
    } finally {
      loadingSeasons = false;
    }
  };

  const fetchEpisodes = async (seriesId: number) => {
    loadingEpisodes = true;
    try {
      episodes = await get_api<EpisodeWithStatus[]>(
        `/api/media/series/${seriesId}/episodes`,
      );
    } catch (err: any) {
      toast.error(`Failed to load episodes: ${err.message}`);
    } finally {
      loadingEpisodes = false;
    }
  };

  const toggleSeason = (id: number) => {
    const next = new Set(selectedSeasonIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedSeasonIds = next;
  };

  const toggleEpisode = (id: number) => {
    const next = new Set(selectedEpisodeIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedEpisodeIds = next;
  };

  const durationOptions = [
    { value: "30", label: "30 days" },
    { value: "90", label: "90 days" },
    { value: "180", label: "180 days" },
    { value: "365", label: "1 year" },
    { value: "custom", label: "Custom days" },
    { value: "permanent", label: "Permanent" },
  ];

  const canSubmit = $derived(
    !!media &&
      (isAdmin || !!reason.trim()) &&
      (!showScopePicker ||
        scopeMode === "series" ||
        (scopeMode === "seasons" && selectedSeasonIds.size > 0) ||
        (scopeMode === "episodes" && selectedEpisodeIds.size > 0)),
  );

  const handleSubmit = async () => {
    if (!media || !canSubmit) return;
    submitting = true;

    try {
      let durationDays: number | null;
      if (duration === "permanent") {
        durationDays = null;
      } else if (duration === "custom") {
        const parsed = Number(customDays);
        if (!Number.isInteger(parsed) || parsed <= 0) {
          toast.error(
            "Custom duration must be a positive whole number of days",
          );
          submitting = false;
          return;
        }
        durationDays = parsed;
      } else {
        durationDays = Number(duration);
      }

      const basePayload = {
        media_type: mediaType,
        media_id: media.id,
        ...(mediaType === MediaType.Movie && selectedVersionId != null
          ? { movie_version_id: selectedVersionId }
          : {}),
        reason: reason.trim() || null,
        duration_days: durationDays,
      };

      if (showScopePicker) {
        // submit one request per checked scope item
        type ScopeExtra = { season_id?: number; episode_id?: number };
        const scopeItems: ScopeExtra[] = [];
        if (scopeMode === "series") {
          scopeItems.push({});
        } else if (scopeMode === "seasons") {
          for (const sid of selectedSeasonIds)
            scopeItems.push({ season_id: sid });
        } else {
          for (const eid of selectedEpisodeIds)
            scopeItems.push({ episode_id: eid });
        }

        const results = await Promise.allSettled(
          scopeItems.map((extra) =>
            post_api<ProtectionRequest>("/api/protection-requests", {
              ...basePayload,
              ...extra,
            }),
          ),
        );

        const succeeded = results.filter(
          (r): r is PromiseFulfilledResult<ProtectionRequest> =>
            r.status === "fulfilled",
        );
        const failed = results.length - succeeded.length;

        if (succeeded.length > 0) {
          toast.success(
            isAdmin
              ? `${succeeded.length} protection${succeeded.length !== 1 ? "s" : ""} created`
              : `${succeeded.length} protection request${succeeded.length !== 1 ? "s" : ""} submitted`,
          );
          succeeded.forEach((r) => onSuccess?.(r.value));
        }
        if (failed > 0)
          toast.error(`${failed} request${failed !== 1 ? "s" : ""} failed`);

        handleClose(false);
      } else {
        // single item (movie, or season sub-row with a pre-set seasonId)
        const req = await post_api<ProtectionRequest>(
          "/api/protection-requests",
          {
            ...basePayload,
            ...(seasonId != null ? { season_id: seasonId } : {}),
            ...(episodeId != null ? { episode_id: episodeId } : {}),
          },
        );

        toast.success(
          isAdmin
            ? `"${media.title}" protected from deletion`
            : "Protection request submitted successfully",
        );
        onSuccess?.(req);
        handleClose(false);
      }
    } catch (err: any) {
      toast.error(`Failed to submit request: ${err.message}`);
    } finally {
      submitting = false;
    }
  };

  const handleClose = (fireCallback: boolean = true) => {
    reason = "";
    duration = isAdmin ? "permanent" : "30";
    customDays = "30";
    open = false;
    if (fireCallback && onClose) onClose();
  };

  const formatSizeGb = (bytes: number | null) =>
    bytes != null ? `${(bytes / 1_000_000_000).toFixed(1)} GB` : null;
</script>

<Dialog.Root bind:open>
  <Dialog.Content
    showCloseButton={false}
    class="sm:max-w-175 max-h-[90vh] overflow-y-auto border-ring border-2"
  >
    <Dialog.Header>
      <Dialog.Title class="text-foreground">
        {isAdmin ? "Protect from Deletion" : "Request Protection"}
      </Dialog.Title>
      <Dialog.Description class="text-muted-foreground">
        {#if seasonId != null && seasonNumber != null}
          {isAdmin ? "Protect" : "Request protection for"} Season {seasonNumber}
          of this series from deletion
        {:else if episodeId != null && episodeNumber != null}
          {isAdmin ? "Protect" : "Request protection for"} episode S{String(
            seasonNumber ?? 0,
          ).padStart(2, "0")}E{String(episodeNumber).padStart(
            2,
            "0",
          )}{#if episodeName}
            &nbsp;&quot;{episodeName}&quot;{/if}
          of this series from deletion
        {:else if showScopePicker}
          {isAdmin
            ? "Choose what to protect from deletion"
            : "Request protection for this series"}
        {:else}
          {isAdmin
            ? `Protect this ${mediaType} from being deleted`
            : `Request that this ${mediaType} be protected from deletion`}
        {/if}
      </Dialog.Description>
    </Dialog.Header>

    {#if media}
      <div class="space-y-4 py-4">
        <!-- media info -->
        <div class="flex gap-4">
          {#if media.poster_url}
            <img
              src="http://image.tmdb.org/t/p/w{TMDB_POSTER_WIDTH}/{media.poster_url}"
              alt={media.title}
              class="w-25 h-38 object-cover rounded"
            />
          {/if}
          <div class="flex-1">
            <h3 class="font-semibold text-foreground">{media.title}</h3>
            <p class="text-sm text-muted-foreground">
              {media.year ?? "Unknown"}
            </p>
            {#if seasonNumber != null}
              <p class="text-sm text-muted-foreground">
                Season {seasonNumber}
              </p>
            {/if}
            {#if episodeNumber != null}
              <p class="text-sm text-muted-foreground">
                S{String(seasonNumber ?? 0).padStart(2, "0")}E{String(
                  episodeNumber,
                ).padStart(2, "0")}{#if episodeName}
                  &nbsp;&quot;{episodeName}&quot;{/if}
              </p>
            {/if}
            {#if media.status.is_candidate}
              <p class="text-xs text-yellow-500 mt-2">
                This {mediaType} is currently marked for deletion
              </p>
            {/if}
          </div>
        </div>

        <!-- version picker (movie with 2+ versions) -->
        {#if showVersionPicker}
          <div class="border border-border rounded-md overflow-hidden">
            <button
              type="button"
              class="w-full flex items-center justify-between px-4 py-2.5
                bg-muted/40 hover:bg-muted/60 transition-colors
                text-sm font-medium text-foreground"
              onclick={() => (versionPickerExpanded = !versionPickerExpanded)}
            >
              <span>Version</span>
              <ChevronRight
                class="size-4 text-muted-foreground transition-transform duration-200
                  {versionPickerExpanded ? 'rotate-90' : ''}"
              />
            </button>

            {#if versionPickerExpanded}
              <div class="px-4 py-3 space-y-1 bg-card">
                <!-- whole movie option -->
                <label
                  class="flex items-center gap-3 py-1.5 px-1 cursor-pointer
                    rounded hover:bg-muted/40"
                >
                  <input
                    type="radio"
                    name="version-pick"
                    checked={selectedVersionId === null}
                    onchange={() => (selectedVersionId = null)}
                    class="accent-primary cursor-pointer"
                  />
                  <span class="text-sm text-foreground font-medium flex-1"
                    >Whole Movie</span
                  >
                </label>

                <div class="border-t border-border pt-1 space-y-0.5">
                  {#each media.versions ?? [] as version (version.id)}
                    <label
                      class="flex items-center gap-3 py-1.5 px-1
                        cursor-pointer rounded hover:bg-muted/40"
                    >
                      <input
                        type="radio"
                        name="version-pick"
                        checked={selectedVersionId === version.id}
                        onchange={() => (selectedVersionId = version.id)}
                        class="accent-primary cursor-pointer"
                      />
                      <span class="text-sm text-foreground flex-1">
                        {version.library_name}{version.path
                          ? " · " + version.path.split(/[\\/]/).pop()
                          : ""}
                      </span>
                      {#if formatSizeGb(version.size) != null}
                        <span class="text-xs text-muted-foreground"
                          >{formatSizeGb(version.size)}</span
                        >
                      {/if}
                    </label>
                  {/each}
                </div>
              </div>
            {/if}
          </div>
        {/if}

        <!-- scope section (series without a pre-selected season) -->
        {#if showScopePicker}
          <div class="border border-border rounded-md overflow-hidden">
            <!-- collapsible header -->
            <button
              type="button"
              class="w-full flex items-center justify-between px-4 py-2.5
                bg-muted/40 hover:bg-muted/60 transition-colors
                text-sm font-medium text-foreground"
              onclick={() => (scopeExpanded = !scopeExpanded)}
            >
              <span>Scope</span>
              <ChevronRight
                class="size-4 text-muted-foreground transition-transform duration-200
                  {scopeExpanded ? 'rotate-90' : ''}"
              />
            </button>

            {#if scopeExpanded}
              <div class="px-4 py-3 space-y-1 bg-card">
                {#if loadingSeasons}
                  <p class="text-sm text-muted-foreground py-1">
                    Loading seasons…
                  </p>
                {:else}
                  <div class="space-y-2">
                    <label
                      class="flex items-center gap-3 py-1.5 px-1 cursor-pointer rounded hover:bg-muted/40"
                    >
                      <input
                        type="radio"
                        name="series-scope"
                        checked={scopeMode === "series"}
                        onchange={() => (scopeMode = "series")}
                        class="accent-primary cursor-pointer"
                      />
                      <span class="text-sm text-foreground font-medium flex-1">
                        Whole series
                      </span>
                    </label>

                    <label
                      class="flex items-center gap-3 py-1.5 px-1 cursor-pointer rounded hover:bg-muted/40"
                    >
                      <input
                        type="radio"
                        name="series-scope"
                        checked={scopeMode === "seasons"}
                        onchange={() => (scopeMode = "seasons")}
                        class="accent-primary cursor-pointer"
                      />
                      <span class="text-sm text-foreground font-medium flex-1">
                        Specific seasons
                      </span>
                    </label>

                    {#if scopeMode === "seasons"}
                      {#if loadingSeasons}
                        <p class="text-sm text-muted-foreground py-1">
                          Loading seasons...
                        </p>
                      {:else if seasons.length === 0}
                        <p class="text-sm text-muted-foreground py-1">
                          No seasons available.
                        </p>
                      {:else}
                        <div class="border-t border-border pt-1 space-y-0.5">
                          {#each seasons as season (season.id)}
                            <label
                              class={`flex items-center gap-3 py-1.5 px-1 rounded ${
                                season.status.is_protected ||
                                season.status.has_pending_request
                                  ? "cursor-not-allowed opacity-60"
                                  : "cursor-pointer hover:bg-muted/40"
                              }`}
                            >
                              <input
                                type="checkbox"
                                checked={selectedSeasonIds.has(season.id)}
                                onchange={() => toggleSeason(season.id)}
                                class="accent-primary cursor-pointer"
                                disabled={season.status.is_protected ||
                                  season.status.has_pending_request}
                              />
                              <span class="text-sm text-foreground flex-1">
                                Season {season.season_number}
                              </span>
                              {#if season.status.is_candidate}
                                <span
                                  class="text-xs text-amber-400 font-medium px-1.5 py-0.5 bg-amber-400/10 rounded"
                                >
                                  flagged
                                </span>
                              {/if}
                              {#if season.status.is_protected}
                                <span class="text-xs text-muted-foreground">
                                  protected
                                </span>
                              {:else if season.status.has_pending_request}
                                <span class="text-xs text-muted-foreground">
                                  pending
                                </span>
                              {/if}
                              {#if formatSizeGb(season.size) != null}
                                <span class="text-xs text-muted-foreground">
                                  {formatSizeGb(season.size)}
                                </span>
                              {/if}
                            </label>
                          {/each}
                        </div>
                      {/if}
                    {/if}

                    <label
                      class="flex items-center gap-3 py-1.5 px-1 cursor-pointer rounded hover:bg-muted/40"
                    >
                      <input
                        type="radio"
                        name="series-scope"
                        checked={scopeMode === "episodes"}
                        onchange={() => (scopeMode = "episodes")}
                        class="accent-primary cursor-pointer"
                      />
                      <span class="text-sm text-foreground font-medium flex-1">
                        Specific episodes
                      </span>
                    </label>

                    {#if scopeMode === "episodes"}
                      {#if loadingEpisodes}
                        <p class="text-sm text-muted-foreground py-1">
                          Loading episodes...
                        </p>
                      {:else if episodes.length === 0}
                        <p class="text-sm text-muted-foreground py-1">
                          No episodes available.
                        </p>
                      {:else}
                        <div
                          class="border-t border-border pt-2 space-y-2 max-h-72 overflow-y-auto pr-1"
                        >
                          {#each Array.from(episodes.reduce((map, ep) => {
                              const current = map.get(ep.season_number) ?? [];
                              current.push(ep);
                              map.set(ep.season_number, current);
                              return map;
                            }, new Map<number, EpisodeWithStatus[]>())) as [groupSeasonNumber, seasonEpisodes]}
                            <div class="space-y-1">
                              <div
                                class="text-xs uppercase tracking-wide text-muted-foreground"
                              >
                                Season {groupSeasonNumber}
                              </div>
                              {#each seasonEpisodes as ep (ep.id)}
                                <label
                                  class={`flex items-center gap-3 py-1.5 px-1 rounded ${
                                    ep.status.is_protected ||
                                    ep.status.has_pending_request
                                      ? "cursor-not-allowed opacity-60"
                                      : "cursor-pointer hover:bg-muted/40"
                                  }`}
                                >
                                  <input
                                    type="checkbox"
                                    checked={selectedEpisodeIds.has(ep.id)}
                                    onchange={() => toggleEpisode(ep.id)}
                                    class="accent-primary cursor-pointer"
                                    disabled={ep.status.is_protected ||
                                      ep.status.has_pending_request}
                                  />
                                  <span class="text-sm text-foreground flex-1">
                                    S{String(ep.season_number).padStart(
                                      2,
                                      "0",
                                    )}E{String(ep.episode_number).padStart(
                                      2,
                                      "0",
                                    )}{#if ep.name}
                                      &nbsp;&quot;{ep.name}&quot;{/if}
                                  </span>
                                  {#if ep.status.is_candidate}
                                    <span
                                      class="text-xs text-amber-400 font-medium px-1.5 py-0.5 bg-amber-400/10 rounded"
                                    >
                                      flagged
                                    </span>
                                  {/if}
                                  {#if ep.status.is_protected}
                                    <span class="text-xs text-muted-foreground">
                                      protected
                                    </span>
                                  {:else if ep.status.has_pending_request}
                                    <span class="text-xs text-muted-foreground">
                                      pending
                                    </span>
                                  {/if}
                                  {#if formatSizeGb(ep.size) != null}
                                    <span class="text-xs text-muted-foreground">
                                      {formatSizeGb(ep.size)}
                                    </span>
                                  {/if}
                                </label>
                              {/each}
                            </div>
                          {/each}
                        </div>
                      {/if}
                    {/if}
                  </div>
                {/if}
              </div>
            {/if}
          </div>
        {/if}

        <!-- reason input (non-admin) -->
        {#if !isAdmin}
          <div class="space-y-2">
            <Label for="reason" class="text-foreground">
              <span class="text-red-500">*</span>
            </Label>
            <Textarea
              id="reason"
              bind:value={reason}
              placeholder={inputPlaceHolderText}
              class="w-full min-h-30 px-3 py-2 bg-card text-card-foreground
                placeholder:text-muted-foreground focus:ring-1 focus:ring-focus-ring resize-none"
              disabled={submitting}
            ></Textarea>
            <p class="text-xs text-muted-foreground">
              Your request will be reviewed by an administrator
            </p>
          </div>
        {/if}

        <!-- duration -->
        <div class="space-y-2">
          <Label class="text-foreground">
            {isAdmin ? "Exclusion duration" : "Protection duration"}
          </Label>
          <Select.Root type="single" bind:value={duration}>
            <Select.Trigger class="w-full">
              {durationOptions.find((opt) => opt.value === duration)?.label}
            </Select.Trigger>
            <Select.Content>
              {#each durationOptions as option}
                <Select.Item
                  value={option.value}
                  label={option.label}
                  class="text-foreground"
                >
                  {option.label}
                </Select.Item>
              {/each}
            </Select.Content>
          </Select.Root>
          {#if duration === "custom"}
            <Input
              type="number"
              min={1}
              step={1}
              bind:value={customDays}
              placeholder="Enter number of days"
              class="input-hover-el"
              disabled={submitting}
            />
          {/if}
          {#if !isAdmin}
            <p class="text-xs text-muted-foreground">
              Admins can override this duration when approving
            </p>
          {/if}
        </div>
      </div>

      <Dialog.Footer>
        <Button
          variant="secondary"
          class="cursor-pointer"
          onclick={() => handleClose()}
          disabled={submitting}
        >
          Cancel
        </Button>
        <Button
          class="cursor-pointer"
          onclick={handleSubmit}
          disabled={submitting || !canSubmit}
        >
          {#if submitting}
            {isAdmin ? "Protecting..." : "Submitting..."}
          {:else}
            {isAdmin ? "Protect from Deletion" : "Request Protection"}
          {/if}
        </Button>
      </Dialog.Footer>
    {/if}
  </Dialog.Content>
</Dialog.Root>
