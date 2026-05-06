<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import SeasonMediaInfoWidget from "$lib/components/candidates/season-mediainfo.svelte";
  import CandidateTmdbMeta from "$lib/components/candidates/candidate-tmdb-meta.svelte";
  import CandidateActionButtons from "$lib/components/candidates/candidate-action-buttons.svelte";
  import { MediaType, type ReclaimCandidateEntry } from "$lib/types/shared";
  import { formatFileSize } from "$lib/utils/formatters";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import X from "@lucide/svelte/icons/x";

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

  const parseRuleTokens = (tokens: string[] | null | undefined): string[] =>
    (tokens ?? []).map((token) => token.trim()).filter(Boolean);

  const seasonResolutionLabel = (entry: ReclaimCandidateEntry): string =>
    entry.season_max_video_height && entry.season_max_video_height > 0
      ? `${entry.season_max_video_height}p`
      : unknownValue;

  const seasonQuickBadges = (entry: ReclaimCandidateEntry): string[] => {
    const badges: string[] = [seasonResolutionLabel(entry)];
    if (entry.season_has_dolby_vision) badges.push("DV");
    else if (entry.season_has_hdr) badges.push("HDR");
    return badges;
  };

  const rulePreview = (entry: ReclaimCandidateEntry): string[] =>
    parseRuleTokens(entry.reason_tokens).slice(0, 2);

  const extraRuleCount = (entry: ReclaimCandidateEntry): number =>
    Math.max(0, parseRuleTokens(entry.reason_tokens).length - 2);

  const openInfo = (entry: ReclaimCandidateEntry) => {
    infoTarget = entry;
    infoOpen = true;
  };

  const closeInfo = () => {
    infoOpen = false;
    infoTarget = null;
  };
</script>

<div class="md:hidden divide-y divide-border">
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
                <CandidateTmdbMeta entry={row.seriesEntry ?? row.seasons[0]} />
              </div>
            </div>
          </button>
        </div>
        {#if expanded}
          <div class="pl-7 space-y-2">
            {#each row.seasons as season (season.id)}
              {@const preview = rulePreview(season)}
              {@const extraCount = extraRuleCount(season)}
              <div
                class="rounded-md border border-border bg-muted/30 p-3 space-y-2"
              >
                <div class="flex items-start justify-between gap-2">
                  <div class="text-sm font-medium text-foreground">
                    Season {season.season_number}
                  </div>
                  <div class="text-xs text-muted-foreground">
                    {formatDate(season.created_at)}
                  </div>
                </div>
                <div class="flex flex-wrap gap-1.5">
                  {#each seasonQuickBadges(season) as badge}
                    <span
                      class="text-xs leading-5 px-2 rounded-full border border-border bg-card text-foreground"
                    >
                      {badge}
                    </span>
                  {/each}
                  <span
                    class="text-xs leading-5 px-2 rounded-full border border-border bg-card text-foreground"
                  >
                    {formatFileSize(season.estimated_space_bytes)}
                  </span>
                </div>
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

<div class="hidden md:block overflow-x-auto">
  <table class="w-full">
    <thead class="bg-muted/50">
      <tr>
        {#if canBulkSelect}
          <th class="px-4 py-3 w-10">
            <input
              type="checkbox"
              checked={allPageSelected}
              onchange={toggleSelectAll}
              class="cursor-pointer accent-primary"
            />
          </th>
        {/if}
        <th
          class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider w-88"
        >
          Media
        </th>
        <th
          class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
        >
          Summary
        </th>
        <th
          class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
        >
          Size
        </th>
        <th
          class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
        >
          Flagged
        </th>
        <th
          class="px-6 py-3 text-right text-xs font-medium text-muted-foreground uppercase tracking-wider"
        >
          Actions
        </th>
      </tr>
    </thead>
    <tbody class="divide-y divide-border">
      {#each rows as row (row.kind === "flat" ? `flat-${row.entry.id}` : `group-${row.media_id}`)}
        {#if row.kind === "flat"}
          {@const entry = row.entry}
          <tr
            class="hover:bg-muted/30 transition-colors {selectedIds.has(
              entry.id,
            )
              ? 'bg-primary/5'
              : ''}"
          >
            {#if canBulkSelect}
              <td class="px-4 py-4 w-10">
                <input
                  type="checkbox"
                  checked={selectedIds.has(entry.id)}
                  onchange={() => toggleSelect(entry.id)}
                  class="cursor-pointer accent-primary"
                />
              </td>
            {/if}
            <td class="px-6 py-4">
              <div class="flex gap-4 items-center min-w-88">
                <div class="flex flex-col items-center w-max gap-1">
                  <PosterThumb
                    mediaType={entry.media_type}
                    posterUrl={entry.poster_url}
                  />
                  <MediaTypeBadge mediaType={entry.media_type} />
                </div>
                <div class="min-w-0 flex-1 text-sm font-medium text-foreground">
                  {entry.media_title}
                  {#if entry.media_year}
                    <span class="text-muted-foreground"
                      >({entry.media_year})</span
                    >
                  {/if}
                  <CandidateTmdbMeta {entry} />
                </div>
              </div>
            </td>
            <td
              class="px-6 py-3 text-sm text-muted-foreground whitespace-normal wrap-break-word"
            >
              <div class="flex flex-wrap gap-1.5">
                <span
                  class="text-xs leading-5 px-2 rounded-full border border-border bg-muted/50 text-foreground"
                >
                  {formatFileSize(entry.estimated_space_bytes)}
                </span>
              </div>
            </td>
            <td class="px-6 py-4 text-sm text-foreground whitespace-nowrap"
              >{formatFileSize(entry.estimated_space_bytes)}</td
            >
            <td
              class="px-6 py-4 text-sm text-muted-foreground whitespace-nowrap"
              >{formatDate(entry.created_at)}</td
            >
            <td class="px-6 py-4 text-right whitespace-nowrap">
              <div class="flex gap-2 justify-end items-center">
                <CandidateActionButtons
                  {entry}
                  {canDelete}
                  {moveEnabled}
                  {openSingleRequest}
                  {openSingleDelete}
                  {openSingleMove}
                  showTooltips
                />
              </div>
            </td>
          </tr>
        {:else}
          {@const expanded = expandedGroups.has(row.media_id)}
          {@const allSel = isGroupAllSelected(row)}
          {@const partSel = isGroupPartialSelected(row)}
          <tr
            class="hover:bg-muted/30 transition-colors cursor-pointer {allSel
              ? 'bg-primary/5'
              : ''}"
            onclick={() => toggleExpand(row.media_id)}
          >
            {#if canBulkSelect}
              <td class="px-4 py-4 w-10" onclick={(e) => e.stopPropagation()}>
                <input
                  type="checkbox"
                  checked={allSel}
                  indeterminate={partSel}
                  onchange={() => toggleGroupSelect(row)}
                  class="cursor-pointer accent-primary"
                />
              </td>
            {/if}
            <td class="px-6 py-4">
              <div class="flex gap-4 items-center min-w-88">
                <div class="flex flex-col items-center w-max gap-1">
                  <PosterThumb
                    mediaType={MediaType.Series}
                    posterUrl={row.poster_url}
                  />
                  <MediaTypeBadge mediaType={MediaType.Series} />
                </div>
                <div class="min-w-0 flex-1 text-sm font-medium text-foreground">
                  {row.media_title}
                  {#if row.media_year}
                    <span class="text-muted-foreground">({row.media_year})</span
                    >
                  {/if}
                  <div class="mt-0.5">
                    <span class="text-xs text-amber-400 font-normal">
                      {row.seasons.length} season{row.seasons.length !== 1
                        ? "s"
                        : ""} flagged
                    </span>
                  </div>
                  <CandidateTmdbMeta
                    entry={row.seriesEntry ?? row.seasons[0]}
                  />
                </div>
              </div>
            </td>
            <td
              class="px-6 py-3 text-sm text-muted-foreground whitespace-normal wrap-break-word"
            >
              <div class="text-xs">{groupSummary(row)}</div>
            </td>
            <td class="px-6 py-4 text-sm text-foreground whitespace-nowrap"
              >{formatFileSize(groupTotalBytes(row))}</td
            >
            <td
              class="px-6 py-4 text-sm text-muted-foreground whitespace-nowrap"
              >{formatDate(row.seasons[0].created_at)}</td
            >
            <td class="px-6 py-4 text-right whitespace-nowrap">
              <div class="flex gap-2 justify-end items-center">
                <ChevronRight
                  class="size-4 text-muted-foreground transition-transform duration-200 {expanded
                    ? 'rotate-90'
                    : ''}"
                />
              </div>
            </td>
          </tr>
          {#if expanded}
            {#each row.seasons as season (season.id)}
              {@const preview = rulePreview(season)}
              {@const extraCount = extraRuleCount(season)}
              <tr
                class="bg-muted/20 border-l-2 border-l-amber-500/40 hover:bg-muted/40
                  transition-colors {selectedIds.has(season.id)
                  ? 'bg-primary/5'
                  : ''}"
              >
                {#if canBulkSelect}
                  <td class="px-4 py-3 w-10 pl-8">
                    <input
                      type="checkbox"
                      checked={selectedIds.has(season.id)}
                      onchange={() => toggleSelect(season.id)}
                      class="cursor-pointer accent-primary"
                    />
                  </td>
                {/if}
                <td class="px-6 py-3 pl-14">
                  <div class="text-sm font-medium text-foreground">
                    Season {season.season_number}
                  </div>
                  <div class="mt-1 flex flex-wrap gap-1.5">
                    {#each seasonQuickBadges(season) as badge}
                      <span
                        class="text-xs leading-5 px-2 rounded-full border border-border bg-card text-foreground"
                      >
                        {badge}
                      </span>
                    {/each}
                  </div>
                </td>
                <td
                  class="px-6 py-3 text-sm text-muted-foreground whitespace-normal wrap-break-word"
                >
                  <div class="flex flex-wrap gap-1.5">
                    {#if preview.length > 0}
                      {#each preview as rule}
                        <span
                          class="text-xs leading-5 px-2 rounded-2xl border border-border
                            bg-muted/50 text-foreground"
                        >
                          {rule}
                        </span>
                      {/each}
                      {#if extraCount > 0}
                        <span
                          class="text-xs leading-5 px-2 rounded-full border border-border bg-muted/50
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
                </td>
                <td class="px-6 py-3 text-sm text-foreground whitespace-nowrap"
                  >{formatFileSize(season.estimated_space_bytes)}</td
                >
                <td
                  class="px-6 py-3 text-sm text-muted-foreground whitespace-nowrap"
                  >{formatDate(season.created_at)}</td
                >
                <td class="px-6 py-3 text-right whitespace-nowrap">
                  <div class="flex gap-2 justify-end items-center">
                    <CandidateActionButtons
                      entry={season}
                      {canDelete}
                      {moveEnabled}
                      {openSingleRequest}
                      {openSingleDelete}
                      {openSingleMove}
                      onInfo={openInfo}
                      showTooltips
                      compact
                    />
                  </div>
                </td>
              </tr>
            {/each}
          {/if}
        {/if}
      {/each}
    </tbody>
  </table>
</div>
