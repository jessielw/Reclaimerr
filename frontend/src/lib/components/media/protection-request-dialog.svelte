<script lang="ts">
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { get_api, post_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import { auth } from "$lib/stores/auth";
  import {
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

  interface MediaLike {
    id: number;
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
    onClose?: () => void;
    onSuccess?: (request: ProtectionRequest) => void;
  }

  let {
    open = $bindable(),
    media,
    mediaType,
    seasonId = null,
    seasonNumber = null,
    onClose,
    onSuccess,
  }: Props = $props();

  const isAdmin = $derived(
    $auth.user?.role === "admin" ||
      ($auth.user?.permissions ?? []).includes(Permission.AutoApprove),
  );

  // show the season checklist only for series without a pre-selected season
  const showScopePicker = $derived(
    mediaType === MediaType.Series && seasonId == null,
  );

  // form state
  let reason = $state("");
  let submitting = $state(false);
  let duration = $state("30");
  let customDays = $state("30");

  // scope state
  let seasons = $state<SeasonWithStatus[]>([]);
  let loadingSeasons = $state(false);
  let scopeExpanded = $state(true);
  let scopeWholeSeries = $state(false);
  let selectedSeasonIds = $state<Set<number>>(new Set());

  $effect(() => {
    if (open) {
      reason = isAdmin ? "Admin decision" : "";
      duration = isAdmin ? "permanent" : "30";
      customDays = "30";
      scopeWholeSeries = false;
      selectedSeasonIds = new Set();
      scopeExpanded = true;
      seasons = [];
      if (showScopePicker && media) {
        fetchSeasons(media.id);
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

  const toggleSeason = (id: number) => {
    const next = new Set(selectedSeasonIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedSeasonIds = next;
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
      (!showScopePicker || scopeWholeSeries || selectedSeasonIds.size > 0),
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
        reason: reason.trim() || null,
        duration_days: durationDays,
      };

      if (showScopePicker) {
        // submit one request per checked scope item
        type ScopeExtra = { season_id?: number };
        const scopeItems: ScopeExtra[] = [];
        if (scopeWholeSeries) scopeItems.push({});
        for (const sid of selectedSeasonIds)
          scopeItems.push({ season_id: sid });

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
    class="media-dialog sm:max-w-175 max-h-[90vh] overflow-y-auto border-ring border-2"
  >
    <Dialog.Header>
      <Dialog.Title class="text-foreground">
        {isAdmin ? "Protect from Deletion" : "Request Protection"}
      </Dialog.Title>
      <Dialog.Description class="text-muted-foreground">
        {#if seasonId != null && seasonNumber != null}
          {isAdmin ? "Protect" : "Request protection for"} Season {seasonNumber}
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
            <p class="text-sm text-muted-foreground">{media.year}</p>
            {#if seasonNumber != null}
              <p class="text-sm text-muted-foreground">
                Season {seasonNumber}
              </p>
            {/if}
            {#if media.status.is_candidate}
              <p class="text-xs text-yellow-500 mt-2">
                This {mediaType} is currently marked for deletion
              </p>
            {/if}
          </div>
        </div>

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
                  <!-- whole series row -->
                  <label
                    class="flex items-center gap-3 py-1.5 px-1 cursor-pointer
                      rounded hover:bg-muted/40"
                  >
                    <input
                      type="checkbox"
                      bind:checked={scopeWholeSeries}
                      class="accent-primary cursor-pointer"
                    />
                    <span class="text-sm text-foreground font-medium flex-1">
                      Whole series
                    </span>
                  </label>

                  {#if seasons.length > 0}
                    <div class="border-t border-border pt-1 space-y-0.5">
                      {#each seasons as season (season.id)}
                        <label
                          class="flex items-center gap-3 py-1.5 px-1
                            cursor-pointer rounded hover:bg-muted/40"
                        >
                          <input
                            type="checkbox"
                            checked={selectedSeasonIds.has(season.id)}
                            onchange={() => toggleSeason(season.id)}
                            class="accent-primary cursor-pointer"
                          />
                          <span class="text-sm text-foreground flex-1">
                            Season {season.season_number}
                          </span>
                          {#if season.status.is_candidate}
                            <span
                              class="text-xs text-amber-400 font-medium px-1.5
                                py-0.5 bg-amber-400/10 rounded"
                            >
                              flagged
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
