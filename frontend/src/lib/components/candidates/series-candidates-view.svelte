<script lang="ts">
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
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
  import Badge from "$lib/components/ui/badge/badge.svelte";
  import CandidateFlatCard from "$lib/components/candidates/candidate-flat-card.svelte";
  import CandidateSeasonInfoDialog from "$lib/components/candidates/candidate-season-info-dialog.svelte";
  import {
    UNKNOWN_VALUE,
    groupEpisodesBySeason,
    newestCandidateCreatedAt,
    seriesGroupCountLabel,
  } from "$lib/components/candidates/view-utils";
  import type {
    FlatRow,
    SeriesGroupRow,
  } from "$lib/components/candidates/view-types";

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

  let infoTarget = $state<ReclaimCandidateEntry | null>(null);

  // per season collapsible state inside an expanded series group
  // key format: "${media_id}-${season_number}"
  let expandedSeasons = $state(new Set<string>());

  const toggleSeasonExpand = (key: string) => {
    const next = new Set(expandedSeasons);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    expandedSeasons = next;
  };

  const openInfo = (entry: ReclaimCandidateEntry) => {
    infoTarget = entry;
  };

  const closeInfo = () => {
    infoTarget = null;
  };
</script>

<div class="divide-y divide-border">
  {#each rows as row (row.kind === "flat" ? `mobile-flat-${row.entry.id}` : `mobile-group-${row.media_id}`)}
    {#if row.kind === "flat"}
      {@const entry = row.entry}
      <CandidateFlatCard
        {entry}
        {canBulkSelect}
        {canDelete}
        {selectedIds}
        {toggleSelect}
        {openSingleRequest}
        {openSingleDelete}
        {openSingleMove}
        {moveEnabled}
        {formatDate}
        posterSize={"154"}
        tailWindElSize="w-24"
      >
        {#snippet summary()}
          <span
            class="text-xs leading-5 px-2 rounded-full border border-border bg-muted/50 text-foreground"
          >
            {formatFileSize(entry.estimated_space_bytes)}
          </span>
        {/snippet}
      </CandidateFlatCard>
    {:else}
      {@const expanded = expandedGroups.has(row.media_id)}
      {@const allSel = isGroupAllSelected(row)}
      {@const partSel = isGroupPartialSelected(row)}
      {@const allRules = groupRuleNames(row.seasons)}
      {@const groupCountLabel = seriesGroupCountLabel(row.seasons)}
      {@const groupDateAdded = newestCandidateCreatedAt(
        row.seriesEntry ? [row.seriesEntry, ...row.seasons] : row.seasons,
      )}
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
                posterSize={"154"}
                tailWindElSize="w-28"
                showMediaType={true}
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
                    {groupCountLabel}
                  </span>
                  <ChevronRight
                    class={`size-4 text-muted-foreground transition-transform ${expanded ? "rotate-90" : ""}`}
                  />
                </div>
                <div class="mt-2 text-xs text-muted-foreground">
                  Total: {formatFileSize(groupTotalBytes(row))}
                </div>
                {#if groupDateAdded}
                  <div class="mt-1 text-xs text-muted-foreground">
                    Date Added: {formatDate(groupDateAdded)}
                  </div>
                {/if}
                <div class="mt-2 flex flex-wrap gap-1.5">
                  {#if allRules.length > 0}
                    {#each allRules as rule}
                      <Badge
                        class="border-primary whitespace-break-spaces"
                        variant="secondary">{rule}</Badge
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
          {@const seasonItems = row.seasons.filter(
            (s) => s.episode_number == null,
          )}
          {@const episodeItems = row.seasons.filter(
            (s) => s.episode_number != null,
          )}
          {@const episodesBySeason = groupEpisodesBySeason(episodeItems)}
          <div class="pl-7 space-y-2">
            {#if seasonItems.length > 0}
              <h2 class="text-xs uppercase tracking-wide text-muted-foreground">
                Seasons
              </h2>
              {#each seasonItems as season (season.id)}
                {@const preview = rulePreview(season)}
                {@const extraCount = extraRuleCount(season)}
                <!-- Season-level candidate row -->
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
                          : UNKNOWN_VALUE}
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
            {/if}

            {#if episodesBySeason.length > 0}
              <h2
                class="text-xs uppercase tracking-wide text-muted-foreground {seasonItems.length >
                0
                  ? 'mt-2'
                  : ''}"
              >
                Episodes
              </h2>
              {#each episodesBySeason as [seasonNum, episodes]}
                {@const seasonKey = `${row.media_id}-${seasonNum}`}
                {@const isSeasonOpen = expandedSeasons.has(seasonKey)}
                {@const seasonTotalBytes = episodes.reduce(
                  (acc, e) => acc + (e.estimated_space_bytes ?? 0),
                  0,
                )}
                <!-- Collapsible per-season episode group header -->
                <button
                  type="button"
                  onclick={() => toggleSeasonExpand(seasonKey)}
                  class="w-full flex items-center gap-2 px-3 py-2 rounded-md border border-border/60
                    bg-muted/20 hover:bg-muted/40 transition-colors text-xs"
                >
                  <ChevronRight
                    class={`size-3.5 text-muted-foreground transition-transform ${isSeasonOpen ? "rotate-90" : ""}`}
                  />
                  <span class="text-foreground font-medium"
                    >Season {seasonNum}</span
                  >
                  <span class="text-amber-400">
                    {episodes.length} episode{episodes.length !== 1 ? "s" : ""}
                  </span>
                  <span class="ml-auto text-muted-foreground">
                    {formatFileSize(seasonTotalBytes)}
                  </span>
                </button>

                {#if isSeasonOpen}
                  <div class="pl-4 space-y-2">
                    {#each episodes as ep (ep.id)}
                      {@const preview = rulePreview(ep)}
                      {@const extraCount = extraRuleCount(ep)}
                      {@const epLabel = `S${String(ep.season_number ?? 0).padStart(2, "0")}E${String(ep.episode_number).padStart(2, "0")}`}
                      <!-- Episode-level candidate row -->
                      <div
                        class="flex gap-3 rounded-md border border-border bg-muted/30 p-3"
                      >
                        {#if canBulkSelect}
                          <input
                            type="checkbox"
                            checked={selectedIds.has(ep.id)}
                            onchange={() => toggleSelect(ep.id)}
                            class="mt-0.5 cursor-pointer accent-primary"
                          />
                        {/if}
                        <div class="flex-1 space-y-2">
                          <div class="space-y-1 text-xs">
                            <div
                              class="tracking-wide text-muted-foreground flex items-start justify-between gap-2"
                            >
                              EPISODE
                              <div class="text-xs text-muted-foreground">
                                {formatDate(ep.created_at)}
                              </div>
                            </div>
                            <div class="text-foreground font-mono">
                              {epLabel}
                              {#if ep.episode_name}
                                <span class="font-sans text-muted-foreground">
                                  - {ep.episode_name}
                                </span>
                              {/if}
                            </div>
                          </div>

                          {#if ep.estimated_space_bytes}
                            <div class="space-y-1 text-xs">
                              <div class="tracking-wide text-muted-foreground">
                                SIZE
                              </div>
                              <div class="text-foreground">
                                {formatFileSize(ep.estimated_space_bytes)}
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
                                  <Badge
                                    class="border-primary"
                                    variant="secondary">{rule}</Badge
                                  >
                                {/each}
                                {#if extraCount > 0}
                                  <span
                                    class="text-xs leading-5 px-2 rounded-2xl border border-border bg-card text-muted-foreground"
                                  >
                                    +{extraCount} more
                                  </span>
                                {/if}
                              </div>
                            </div>
                          {/if}
                          <div class="flex justify-end gap-2">
                            <CandidateActionButtons
                              entry={ep}
                              {canDelete}
                              {moveEnabled}
                              {openSingleRequest}
                              {openSingleDelete}
                              {openSingleMove}
                              compact
                            />
                          </div>
                        </div>
                      </div>
                    {/each}
                  </div>
                {/if}
              {/each}
            {/if}
          </div>
        {/if}
      </div>
    {/if}
  {/each}
</div>

<CandidateSeasonInfoDialog
  entry={infoTarget}
  {formatDate}
  onClose={closeInfo}
/>
