<script lang="ts">
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { post_api, get_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import { auth } from "$lib/stores/auth";
  import {
    type EpisodeWithStatus,
    MediaType,
    Permission,
    type DeleteRequest,
    type MovieVersion,
    type SeasonWithStatus,
  } from "$lib/types/shared";
  import { Textarea } from "$lib/components/ui/textarea/index.js";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import { formatFileSize } from "$lib/utils/formatters";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";

  const TMDB_POSTER_WIDTH = 342;
  const inputPlaceHolderText =
    "Explain why this should be deleted (e.g., duplicate copy, no longer wanted, wrong version).";

  interface MediaLike {
    id: number;
    title: string;
    year: number | null;
    poster_url: string | null;
    status: {
      is_protected: boolean;
      has_pending_delete_request: boolean;
    };
    versions?: MovieVersion[];
  }

  interface Props {
    open: boolean;
    media: MediaLike | null;
    mediaType: MediaType;
    onClose?: () => void;
    onSuccess?: (request: DeleteRequest) => void;
  }

  let {
    open = $bindable(),
    media,
    mediaType,
    onClose,
    onSuccess,
  }: Props = $props();

  const isAdmin = $derived($auth.user?.role === "admin");

  const canRequestDelete = $derived(
    isAdmin ||
      ($auth.user?.permissions ?? []).includes(Permission.Request) ||
      ($auth.user?.permissions ?? []).includes(Permission.AutoApprove),
  );

  const showSeasonPicker = $derived(mediaType === MediaType.Series);
  const showVersionPicker = $derived(mediaType === MediaType.Movie);

  let reason = $state("");
  let submitting = $state(false);

  let seasons = $state<SeasonWithStatus[]>([]);
  let episodes = $state<EpisodeWithStatus[]>([]);
  let loadingSeasons = $state(false);
  let loadingEpisodes = $state(false);
  let scopeExpanded = $state(true);
  let scopeMode = $state<"whole" | "versions" | "seasons" | "episodes">(
    "whole",
  );
  let selectedSeasonIds = $state<Set<number>>(new Set());
  let selectedVersionIds = $state<Set<number>>(new Set());
  let selectedEpisodeIds = $state<Set<number>>(new Set());

  $effect(() => {
    if (open) {
      reason = "";
      scopeMode = "whole";
      selectedSeasonIds = new Set();
      selectedVersionIds = new Set();
      selectedEpisodeIds = new Set();
      scopeExpanded = true;
      seasons = [];
      episodes = [];
      if (showSeasonPicker && media) {
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

  const toggleVersion = (id: number) => {
    const next = new Set(selectedVersionIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedVersionIds = next;
  };

  const toggleEpisode = (id: number) => {
    const next = new Set(selectedEpisodeIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedEpisodeIds = next;
  };

  const groupedEpisodes = $derived(
    Array.from(
      episodes.reduce((map, ep) => {
        const current = map.get(ep.season_number) ?? [];
        current.push(ep);
        map.set(ep.season_number, current);
        return map;
      }, new Map<number, EpisodeWithStatus[]>()),
    ).sort((a, b) => a[0] - b[0]),
  );

  const pathBasename = (path: string | null) => {
    if (!path) return "Unknown file";
    const parts = path.split(/[\\/]/);
    return parts[parts.length - 1] || path;
  };

  const canSubmit = $derived(
    !!media &&
      canRequestDelete &&
      (isAdmin || !!reason.trim()) &&
      !media.status.is_protected &&
      !media.status.has_pending_delete_request &&
      (scopeMode === "whole" ||
        (showSeasonPicker &&
          scopeMode === "seasons" &&
          selectedSeasonIds.size > 0) ||
        (showSeasonPicker &&
          scopeMode === "episodes" &&
          selectedEpisodeIds.size > 0) ||
        (showVersionPicker &&
          scopeMode === "versions" &&
          selectedVersionIds.size > 0)),
  );

  const handleSubmit = async () => {
    if (!media || !canSubmit) return;
    submitting = true;

    try {
      const basePayload = {
        media_type: mediaType,
        media_id: media.id,
        reason: reason.trim(),
      };

      const scopePayloads: Array<Record<string, number>> = [];
      if (scopeMode === "whole") scopePayloads.push({});
      if (showSeasonPicker && scopeMode === "seasons") {
        for (const seasonId of selectedSeasonIds) {
          scopePayloads.push({ season_id: seasonId });
        }
      }
      if (showSeasonPicker && scopeMode === "episodes") {
        for (const episodeId of selectedEpisodeIds) {
          scopePayloads.push({ episode_id: episodeId });
        }
      }
      if (showVersionPicker && scopeMode === "versions") {
        for (const versionId of selectedVersionIds) {
          scopePayloads.push({ movie_version_id: versionId });
        }
      }

      const results = await Promise.allSettled(
        scopePayloads.map((scope) =>
          post_api<DeleteRequest>("/api/delete-requests", {
            ...basePayload,
            ...scope,
          }),
        ),
      );

      const succeeded = results.filter(
        (r): r is PromiseFulfilledResult<DeleteRequest> =>
          r.status === "fulfilled",
      );
      const failed = results.length - succeeded.length;

      if (succeeded.length > 0) {
        toast.success(
          `${succeeded.length} delete request${succeeded.length === 1 ? "" : "s"} submitted`,
        );
        succeeded.forEach((r) => onSuccess?.(r.value));
      }
      if (failed > 0) {
        toast.error(
          `${failed} delete request${failed === 1 ? "" : "s"} failed`,
        );
      }

      handleClose(false);
    } catch (err: any) {
      toast.error(`Failed to submit delete request: ${err.message}`);
    } finally {
      submitting = false;
    }
  };

  const handleClose = (fireCallback: boolean = true) => {
    reason = "";
    open = false;
    if (fireCallback && onClose) onClose();
  };
</script>

<Dialog.Root bind:open>
  <Dialog.Content
    showCloseButton={false}
    class="flex flex-col sm:max-w-175 h-[90vh] border-ring border-2"
  >
    <Dialog.Header>
      <Dialog.Title class="text-foreground">Request Deletion</Dialog.Title>
      <Dialog.Description class="text-muted-foreground">
        Submit a deletion request for this media. Admin approval is always
        required.
      </Dialog.Description>
    </Dialog.Header>

    <div class="flex-1 overflow-y-auto px-4 py-4">
      {#if media}
        <div class="space-y-4 py-4">
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
              {#if media.status.is_protected}
                <p class="mt-2 text-xs text-red-500">
                  This item is protected and cannot be requested for deletion.
                </p>
              {/if}
              {#if media.status.has_pending_delete_request}
                <p class="mt-2 text-xs text-blue-500">
                  A delete request is already pending for this item.
                </p>
              {/if}
            </div>
          </div>

          <div class="rounded-xl border border-border/70 bg-card/70">
            <button
              type="button"
              class="flex w-full items-center justify-between px-4 py-3 text-left"
              onclick={() => (scopeExpanded = !scopeExpanded)}
            >
              <div>
                <p class="font-medium text-foreground">Request Scope</p>
                <p class="text-sm text-muted-foreground">
                  Choose the whole item or a narrower target.
                </p>
              </div>
              <ChevronRight
                class={`size-5 text-muted-foreground transition-transform ${scopeExpanded ? "rotate-90" : ""}`}
              />
            </button>

            {#if scopeExpanded}
              <div class="space-y-4 border-t border-border/70 px-4 py-4">
                <label
                  class="flex items-start gap-3 rounded-lg border border-border/60 px-3 py-3"
                >
                  <input
                    type="radio"
                    name="delete-scope"
                    checked={scopeMode === "whole"}
                    onchange={() => (scopeMode = "whole")}
                    class="mt-1"
                  />
                  <div>
                    <Tooltip.Root>
                      <Tooltip.Trigger class="w-full">
                        <p class="font-medium text-foreground">Delete all</p>
                      </Tooltip.Trigger>
                      <Tooltip.Content>
                        <p>
                          {mediaType === MediaType.Movie
                            ? "(Deletes all movie versions)"
                            : "(Deletes all seasons & episodes)"}
                        </p>
                      </Tooltip.Content>
                    </Tooltip.Root>
                  </div>
                </label>

                {#if showVersionPicker}
                  <div class="space-y-2">
                    <label
                      class="flex items-start gap-3 rounded-lg border border-border/60 px-3 py-3"
                    >
                      <input
                        type="radio"
                        name="delete-scope"
                        checked={scopeMode === "versions"}
                        onchange={() => (scopeMode = "versions")}
                        class="mt-1"
                      />
                      <div>
                        <p class="font-medium text-foreground">
                          Specific versions
                        </p>
                      </div>
                    </label>
                    {#if scopeMode === "versions"}
                      {#if (media.versions ?? []).length === 0}
                        <p class="text-sm text-muted-foreground">
                          No versions available.
                        </p>
                      {:else}
                        <div class="max-h-52 overflow-y-auto space-y-2 pr-1">
                          {#each media.versions ?? [] as version (version.id)}
                            <label
                              class="flex items-start gap-3 rounded-lg border border-border/60 px-3 py-3"
                            >
                              <input
                                type="checkbox"
                                checked={selectedVersionIds.has(version.id)}
                                onchange={() => toggleVersion(version.id)}
                                class="mt-1"
                              />
                              <div class="min-w-0">
                                <p class="font-medium text-foreground">
                                  {version.library_name} - {version.service}
                                </p>
                                <p
                                  class="break-all text-sm text-muted-foreground"
                                >
                                  {pathBasename(version.path)}
                                </p>
                                <p class="text-xs text-muted-foreground">
                                  {version.size != null
                                    ? formatFileSize(version.size)
                                    : "Unknown size"}
                                </p>
                              </div>
                            </label>
                          {/each}
                        </div>
                      {/if}
                    {/if}
                  </div>
                {/if}

                {#if showSeasonPicker}
                  <div class="space-y-2">
                    <label
                      class="flex items-start gap-3 rounded-lg border border-border/60 px-3 py-3"
                    >
                      <input
                        type="radio"
                        name="delete-scope"
                        checked={scopeMode === "seasons"}
                        onchange={() => (scopeMode = "seasons")}
                        class="mt-1"
                      />
                      <div>
                        <p class="font-medium text-foreground">
                          Specific seasons
                        </p>
                      </div>
                    </label>
                    {#if scopeMode === "seasons"}
                      {#if loadingSeasons}
                        <p class="text-sm text-muted-foreground">
                          Loading seasons...
                        </p>
                      {:else if seasons.length === 0}
                        <p class="text-sm text-muted-foreground">
                          No seasons available.
                        </p>
                      {:else}
                        <div class="max-h-52 overflow-y-auto space-y-2 pr-1">
                          {#each seasons as season (season.id)}
                            <label
                              class={`flex items-start gap-3 rounded-lg border border-border/60 px-3 py-3 ${season.status.is_protected || season.status.has_pending_delete_request ? "cursor-not-allowed opacity-60" : ""}`}
                            >
                              <input
                                type="checkbox"
                                checked={selectedSeasonIds.has(season.id)}
                                onchange={() => toggleSeason(season.id)}
                                class="mt-1"
                                disabled={season.status.is_protected ||
                                  season.status.has_pending_delete_request}
                              />
                              <div class="min-w-0">
                                <p class="font-medium text-foreground">
                                  Season {season.season_number}
                                </p>
                                <p class="text-sm text-muted-foreground">
                                  {season.episode_count ?? "?"} episodes
                                  {#if season.size != null}
                                    - {formatFileSize(season.size)}
                                  {/if}
                                </p>
                              </div>
                            </label>
                          {/each}
                        </div>
                      {/if}
                    {/if}
                  </div>

                  <div class="space-y-2">
                    <label
                      class="flex items-start gap-3 rounded-lg border border-border/60 px-3 py-3"
                    >
                      <input
                        type="radio"
                        name="delete-scope"
                        checked={scopeMode === "episodes"}
                        onchange={() => (scopeMode = "episodes")}
                        class="mt-1"
                      />
                      <div>
                        <p class="font-medium text-foreground">
                          Specific episodes
                        </p>
                      </div>
                    </label>
                    {#if scopeMode === "episodes"}
                      {#if loadingEpisodes}
                        <p class="text-sm text-muted-foreground">
                          Loading episodes...
                        </p>
                      {:else if episodes.length === 0}
                        <p class="text-sm text-muted-foreground">
                          No episodes available.
                        </p>
                      {:else}
                        <div class="max-h-72 overflow-y-auto space-y-2 pr-1">
                          {#each groupedEpisodes as [groupSeasonNumber, seasonEpisodes]}
                            <div class="space-y-1">
                              <div
                                class="text-xs uppercase tracking-wide text-muted-foreground"
                              >
                                Season {groupSeasonNumber}
                              </div>
                              {#each seasonEpisodes as ep (ep.id)}
                                <label
                                  class={`flex items-start gap-3 rounded-lg border border-border/60 px-3 py-3 ${ep.status.is_protected || ep.status.has_pending_delete_request ? "cursor-not-allowed opacity-60" : ""}`}
                                >
                                  <input
                                    type="checkbox"
                                    checked={selectedEpisodeIds.has(ep.id)}
                                    onchange={() => toggleEpisode(ep.id)}
                                    class="mt-1"
                                    disabled={ep.status.is_protected ||
                                      ep.status.has_pending_delete_request}
                                  />
                                  <div class="min-w-0">
                                    <p class="font-medium text-foreground">
                                      S{String(ep.season_number).padStart(
                                        2,
                                        "0",
                                      )}E{String(ep.episode_number).padStart(
                                        2,
                                        "0",
                                      )}{#if ep.name}
                                        &nbsp;&quot;{ep.name}&quot;{/if}
                                    </p>
                                    <p class="text-sm text-muted-foreground">
                                      {ep.size != null
                                        ? formatFileSize(ep.size)
                                        : "Unknown size"}
                                    </p>
                                  </div>
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

          {#if !isAdmin}
            <div class="space-y-2">
              <Label for="delete-reason" class="text-foreground">
                Reason{isAdmin ? " (optional)" : ""}
              </Label>
              <Textarea
                id="delete-reason"
                bind:value={reason}
                rows={4}
                placeholder={isAdmin
                  ? "Optionally describe why this should be deleted."
                  : inputPlaceHolderText}
                class="input-hover-el text-foreground"
              />
            </div>
          {/if}
        </div>
      {/if}
    </div>

    <Dialog.Footer class="mt-auto">
      <Button
        variant="secondary"
        class="cursor-pointer"
        onclick={() => handleClose()}
      >
        Cancel
      </Button>
      <Button
        onclick={handleSubmit}
        disabled={!canSubmit || submitting}
        class="cursor-pointer"
      >
        {#if submitting}
          {isAdmin ? "Submitting..." : "Requesting..."}
        {:else}
          {isAdmin ? "Submit Deletion" : "Request Deletion"}
        {/if}
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
