<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import SeasonMediaInfoWidget from "$lib/components/candidates/season-mediainfo.svelte";
  import CandidateTmdbMeta from "$lib/components/candidates/candidate-tmdb-meta.svelte";
  import CandidateActionButtons from "$lib/components/candidates/candidate-action-buttons.svelte";
  import { MediaType, type ReclaimCandidateEntry } from "$lib/types/shared";
  import { formatFileSize, cleanResolutionString } from "$lib/utils/formatters";
  import {
    rulePreview,
    extraRuleCount,
    groupRuleNames,
  } from "$lib/utils/candidate-rules";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import X from "@lucide/svelte/icons/x";
  import Badge from "$lib/components/ui/badge/badge.svelte";

  type FlatRow = { kind: "flat"; entry: ReclaimCandidateEntry };
  type SeriesGroupRow = {
    kind: "group";
    group_type: "series_seasons";
    seriesEntry: ReclaimCandidateEntry | null;
    seasons: ReclaimCandidateEntry[];
    versions: ReclaimCandidateEntry[];
    media_id: number;
    media_title: string;
    media_year: number | null;
    poster_url: string | null;
  };
  type DisplayRow = FlatRow | SeriesGroupRow;

  interface Props {
    rows: DisplayRow[];
    canBulkSelect: boolean;
    canDelete: boolean;
    selectedIds: Set<number>;
    expandedGroups: Set<number>;
    allPageSelected: boolean;
    toggleSelect: (id: number) => void;
    toggleSelectAll: () => void;
    toggleGroupSelect: (row: SeriesGroupRow) => void;
    isGroupAllSelected: (row: SeriesGroupRow) => boolean;
    isGroupPartialSelected: (row: SeriesGroupRow) => boolean;
    toggleExpand: (mediaId: number) => void;
    openSingleRequest: (entry: ReclaimCandidateEntry) => void;
    openSingleDelete: (entry: ReclaimCandidateEntry) => void;
    openSingleMove: (entry: ReclaimCandidateEntry) => void;
    moveEnabled: boolean;
    formatDate: (value: string) => string;
    groupTotalBytes: (row: SeriesGroupRow) => number;
  }

  let {
    rows,
    canBulkSelect,
    canDelete,
    selectedIds,
    expandedGroups,
    allPageSelected,
    toggleSelect,
    toggleSelectAll,
    toggleGroupSelect,
    isGroupAllSelected,
    isGroupPartialSelected,
    toggleExpand,
    openSingleRequest,
    openSingleDelete,
    openSingleMove,
    moveEnabled,
    formatDate,
    groupTotalBytes,
  }: Props = $props();

  let infoOpen = $state(false);
  let infoTarget = $state<ReclaimCandidateEntry | null>(null);

  const unknownValue = "Unknown";

  const groupSummary = (row: SeriesGroupRow): string =>
    `Total: ${formatFileSize(groupTotalBytes(row))} - Flagged: ${formatDate(row.seasons[0].created_at)}`;

  const seasonResolutionLabel = (entry: ReclaimCandidateEntry): string => {
    const res = entry.season_max_video_height
      ? String(entry.season_max_video_height)
      : null;
    return cleanResolutionString(res) ?? unknownValue;
  };

  const openInfo = (entry: ReclaimCandidateEntry) => {
    infoTarget = entry;
    infoOpen = true;
  };

  const closeInfo = () => {
    infoOpen = false;
    infoTarget = null;
  };
</script>

<div class="divide-y divide-border">
  {#each rows as row (row.kind === "flat" ? `mobile-flat-${row.entry.id}` : `mobile-group-${row.media_id}`)}
    {#if row.kind === "flat"}
      {@const entry = row.entry}
      <div class="p-4 space-y-3">
        <div class="flex gap-3">
          {#if canBulkSelect}
            <input
              type="checkbox"
              checked={selectedIds.has(entry.id)}
              onchange={() => toggleSelect(entry.id)}
              class="mt-1 cursor-pointer accent-primary"
            />
          {/if}
          <div class="flex-1">
            <div class="flex items-start gap-3">
              <PosterThumb
                mediaType={entry.media_type}
                posterUrl={entry.poster_url}
                posterSize={"154"}
                tailWindElSize="w-24"
              />
              <div class="min-w-0">
                <div class="text-sm font-medium text-foreground">
                  {entry.media_title}
                  {#if entry.media_year}
                    <span class="text-muted-foreground"
                      >({entry.media_year})</span
                    >
                  {/if}
                </div>
                <div class="mt-1">
                  <MediaTypeBadge mediaType={entry.media_type} />
                </div>
                <div class="mt-2 flex flex-wrap gap-1.5">
                  <span
                    class="text-xs leading-5 px-2 rounded-full border border-border bg-muted/50 text-foreground"
                  >
                    {formatFileSize(entry.estimated_space_bytes)}
                  </span>
                </div>
                <div class="mt-2 text-xs text-muted-foreground">
                  {formatDate(entry.created_at)}
                </div>
                <CandidateTmdbMeta {entry} />
              </div>
            </div>
          </div>
        </div>
        <div class="flex justify-end gap-2">
          <CandidateActionButtons
            {entry}
            {canDelete}
            {moveEnabled}
            {openSingleRequest}
            {openSingleDelete}
            {openSingleMove}
          />
        </div>
      </div>
    {:else}
      {@const expanded = expandedGroups.has(row.media_id)}
      {@const allSel = isGroupAllSelected(row)}
      {@const partSel = isGroupPartialSelected(row)}
      {@const allRules = groupRuleNames(row.seasons)}
      <div class="p-4 space-y-3">
        <div class="flex gap-3">
          {#if canBulkSelect}
            <input
              type="checkbox"
              checked={allSel}
              indeterminate={partSel}
              onchange={() => toggleGroupSelect(row)}
              class="mt-1 cursor-pointer accent-primary"
            />
          {/if}
          <button
            type="button"
            class="flex-1 text-left"
            onclick={() => toggleExpand(row.media_id)}
          >
            <div class="flex items-start gap-3">
              <PosterThumb
                mediaType={MediaType.Series}
                posterUrl={row.poster_url}
              />
              <div class="min-w-0">
                <div class="text-sm font-medium text-foreground">
                  {row.media_title}
                  {#if row.media_year}
                    <span class="text-muted-foreground">({row.media_year})</span
                    >
                  {/if}
                </div>
                <div class="mt-1 flex items-center gap-2">
                  <MediaTypeBadge mediaType={MediaType.Series} />
                  <span class="text-xs text-amber-400">
                    {row.seasons.length} season{row.seasons.length !== 1
                      ? "s"
                      : ""}
                  </span>
                  <ChevronRight
                    class={`size-4 text-muted-foreground transition-transform ${expanded ? "rotate-90" : ""}`}
                  />
                </div>
                <div class="mt-2 text-xs text-muted-foreground">
                  {groupSummary(row)}
                </div>
                <div class="mt-2 flex flex-wrap gap-1.5">
                  {#if allRules.length > 0}
                    {#each allRules as rule}
                      <Badge class="border-primary" variant="secondary"
                        >{rule}</Badge
                      >
                    {/each}
                  {/if}
                </div>
                <CandidateTmdbMeta entry={row.seriesEntry ?? row.seasons[0]} />
              </div>
            </div>
          </button>
        </div>
        {#if expanded}
          <div class="pl-7 space-y-2">
            <h2>Seasons</h2>
            {#each row.seasons as season (season.id)}
              {@const preview = rulePreview(season)}
              {@const extraCount = extraRuleCount(season)}
              <div
                class="flex gap-3 rounded-md border border-border bg-muted/30 p-3"
              >
                {#if canBulkSelect}
                  <input
                    type="checkbox"
                    checked={selectedIds.has(season.id)}
                    onchange={() => toggleSelect(season.id)}
                    class="mt-0.5 cursor-pointer accent-primary"
                  />
                {/if}
                <div class="flex-1 space-y-2">
                  <div class="space-y-1 text-xs">
                    <div
                      class="tracking-wide text-muted-foreground flex items-start justify-between gap-2"
                    >
                      SEASON
                      <div class="text-xs text-muted-foreground">
                        {formatDate(season.created_at)}
                      </div>
                    </div>
                    <div class="text-foreground">
                      {season.season_number != null
                        ? `Season ${season.season_number}`
                        : unknownValue}
                    </div>
                  </div>

                  {#if season.season_max_video_height}
                    <div class="space-y-1 text-xs">
                      <div class="tracking-wide text-muted-foreground">
                        RESOLUTION
                      </div>
                      <div class="text-foreground">
                        {cleanResolutionString(
                          String(season.season_max_video_height),
                        )}
                      </div>
                    </div>
                  {/if}

                  {#if season.estimated_space_bytes}
                    <div class="space-y-1 text-xs">
                      <div class="tracking-wide text-muted-foreground">
                        SIZE
                      </div>
                      <div class="text-foreground">
                        {formatFileSize(season.estimated_space_bytes)}
                      </div>
                    </div>
                  {/if}

                  {#if season.series_library_refs?.length}
                    <div class="space-y-1 text-xs">
                      <div class="tracking-wide text-muted-foreground">
                        LIBRARIES
                      </div>
                      <div class="text-foreground break-all">
                        {season.series_library_refs
                          .map((ref) => ref.library_name)
                          .join(", ")}
                      </div>
                    </div>
                  {/if}

                  {#if preview.length > 0}
                    <div class="space-y-1">
                      <div
                        class="text-[11px] uppercase tracking-wide text-muted-foreground"
                      >
                        Matched rules
                      </div>
                      <div class="flex flex-wrap gap-1.5">
                        {#each preview as rule}
                          <Badge class="border-primary" variant="secondary"
                            >{rule}</Badge
                          >
                        {/each}
                        {#if extraCount > 0}
                          <span
                            class="text-xs leading-5 px-2 rounded-2xl border border-border bg-card
                          text-muted-foreground"
                          >
                            +{extraCount} more
                          </span>
                        {/if}
                      </div>
                    </div>
                  {:else}
                    <div class="flex flex-wrap gap-1.5">
                      {#if preview.length > 0}
                        {#each preview as rule}
                          <span
                            class="text-xs leading-5 px-2 rounded-full border border-border bg-card text-foreground"
                          >
                            {rule}
                          </span>
                        {/each}
                        {#if extraCount > 0}
                          <span
                            class="text-xs leading-5 px-2 rounded-full border border-border bg-card
                          text-muted-foreground"
                          >
                            +{extraCount} more
                          </span>
                        {/if}
                      {:else}
                        <span class="text-xs text-muted-foreground"
                          >{unknownValue}</span
                        >
                      {/if}
                    </div>
                  {/if}
                  <div class="flex justify-end gap-2">
                    <CandidateActionButtons
                      entry={season}
                      {canDelete}
                      {moveEnabled}
                      {openSingleRequest}
                      {openSingleDelete}
                      {openSingleMove}
                      onInfo={openInfo}
                      compact
                    />
                  </div>
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
  {/each}
</div>

{#if infoOpen && infoTarget}
  <div class="fixed inset-0 z-50">
    <button
      type="button"
      aria-label="Close details"
      class="absolute inset-0 bg-background/70 backdrop-blur-[1px]"
      onclick={closeInfo}
    ></button>
    <div
      class="relative mx-auto mt-8 w-[min(92vw,820px)] rounded-md border border-border bg-card shadow-xl"
      role="dialog"
      aria-modal="true"
      tabindex="-1"
    >
      <div
        class="flex items-center justify-between border-b border-border px-4 py-3"
      >
        <div class="min-w-0">
          <div class="text-sm font-semibold text-foreground truncate">
            {infoTarget.series_title ?? infoTarget.media_title} - Season {infoTarget.season_number}
          </div>
          <div class="text-xs text-muted-foreground truncate">
            {seasonResolutionLabel(infoTarget)} - {formatFileSize(
              infoTarget.estimated_space_bytes,
            )}
          </div>
        </div>
        <Button
          size="icon"
          class="cursor-pointer rounded-full size-8"
          onclick={closeInfo}
        >
          <X class="size-4" />
        </Button>
      </div>
      <div class="max-h-[75vh] overflow-y-auto p-4">
        <SeasonMediaInfoWidget entry={infoTarget} {formatDate} />
      </div>
    </div>
  </div>
{/if}
