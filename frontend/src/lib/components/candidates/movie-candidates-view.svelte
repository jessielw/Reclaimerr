<script lang="ts">
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import { MediaType, type ReclaimCandidateEntry } from "$lib/types/shared";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import CandidateActionButtons from "$lib/components/candidates/candidate-action-buttons.svelte";
  import CandidateTmdbMeta from "$lib/components/candidates/candidate-tmdb-meta.svelte";
  import { formatFileSize, cleanResolutionString } from "$lib/utils/formatters";
  import {
    rulePreview,
    groupRuleNames,
    extractPathNoFile,
  } from "$lib/utils/candidate-rules";
  import Badge from "$lib/components/ui/badge/badge.svelte";
  import CandidateVersionInfoDialog from "$lib/components/candidates/candidate-version-info-dialog.svelte";
  import CandidateFlatCard from "$lib/components/candidates/candidate-flat-card.svelte";
  import { movieSummaryChips } from "$lib/components/candidates/view-utils";
  import type {
    FlatRow,
    MovieGroupRow,
  } from "$lib/components/candidates/view-types";

  type DisplayRow = FlatRow | MovieGroupRow;

  interface Props {
    rows: DisplayRow[];
    canBulkSelect: boolean;
    canDelete: boolean;
    selectedIds: Set<number>;
    expandedGroups: Set<number>;
    allPageSelected: boolean;
    toggleSelect: (id: number) => void;
    toggleSelectAll: () => void;
    toggleGroupSelect: (row: MovieGroupRow) => void;
    isGroupAllSelected: (row: MovieGroupRow) => boolean;
    isGroupPartialSelected: (row: MovieGroupRow) => boolean;
    toggleExpand: (mediaId: number) => void;
    openSingleRequest: (entry: ReclaimCandidateEntry) => void;
    openSingleDelete: (entry: ReclaimCandidateEntry) => void;
    openSingleMove: (entry: ReclaimCandidateEntry) => void;
    moveEnabled: boolean;
    formatDate: (value: string) => string;
    groupTotalBytes: (row: MovieGroupRow) => number;
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
      >
        {#snippet summary()}
          {#each movieSummaryChips(entry) as chip}
            <span
              class="text-xs leading-5 px-2 rounded-full border border-border bg-muted/50 text-foreground"
            >
              {chip}
            </span>
          {/each}
        {/snippet}
      </CandidateFlatCard>
    {:else}
      {@const expanded = expandedGroups.has(row.media_id)}
      {@const allSel = isGroupAllSelected(row)}
      {@const partSel = isGroupPartialSelected(row)}
      {@const allRules = groupRuleNames(row.versions)}
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
                mediaType={MediaType.Movie}
                posterUrl={row.poster_url}
                posterSize={"154"}
                tailWindElSize="w-28"
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
                  <MediaTypeBadge mediaType={MediaType.Movie} />
                  <span class="text-xs text-amber-400">
                    {row.versions.length} version{row.versions.length !== 1
                      ? "s"
                      : ""}
                  </span>
                  <ChevronRight
                    class={`size-4 text-muted-foreground transition-transform ${expanded ? "rotate-90" : ""}`}
                  />
                </div>
                <div class="mt-2 flex flex-wrap gap-1.5">
                  {#each movieSummaryChips(row.versions[0]) as chip}
                    <span
                      class="text-xs leading-5 px-2 rounded-full border border-border bg-muted/50 text-foreground"
                    >
                      {chip}
                    </span>
                  {/each}
                </div>
                <div class="mt-2 flex flex-wrap gap-1.5">
                  {#if allRules.length > 0}
                    {#each allRules as rule}
                      <Badge
                        class="border-primary whitespace-break-spaces"
                        variant="secondary">{rule}</Badge
                      >
                    {/each}
                    <!-- {:else}
                    <span
                      class="text-xs leading-5 px-2 rounded-2xl border border-border bg-card text-muted-foreground"
                    >
                      {unknownValue}
                    </span> -->
                  {/if}
                </div>
                <CandidateTmdbMeta entry={row.versions[0]} />
              </div>
            </div>
          </button>
        </div>
        {#if expanded}
          <div class="pl-7 space-y-2">
            <h2>Versions</h2>
            {#each row.versions as version (version.id)}
              {@const previewRules = rulePreview(version)}
              <!-- {@const extraCount = extraRuleCount(version)}
              {@const reasons = detailReasons(version)} -->
              <!-- {console.log(version)} -->
              <div
                class="flex gap-3 rounded-md border border-border bg-muted/30 p-3"
              >
                {#if canBulkSelect}
                  <input
                    type="checkbox"
                    checked={selectedIds.has(version.id)}
                    onchange={() => toggleSelect(version.id)}
                    class="mt-0.5 cursor-pointer accent-primary"
                  />
                {/if}
                <div class="flex-1 space-y-2">
                  <!-- file name -->
                  {#if version.version_file_name}
                    <div class="space-y-1 text-xs">
                      <div
                        class="tracking-wide text-muted-foreground flex items-start justify-between gap-2"
                      >
                        FILE NAME
                        <div class="text-xs text-muted-foreground">
                          {formatDate(version.created_at)}
                        </div>
                      </div>
                      <div class="text-foreground break-all">
                        {version.version_file_name}
                      </div>
                    </div>
                  {/if}

                  <!-- path without file -->
                  {#if version.version_path}
                    <div class="space-y-1 text-xs">
                      <div class="tracking-wide text-muted-foreground">
                        PATH
                      </div>
                      <div class="text-foreground break-all">
                        {extractPathNoFile(version.version_path)}
                      </div>
                    </div>
                  {/if}

                  <!-- resolution -->
                  {#if version.version_video_resolution}
                    <div class="space-y-1 text-xs">
                      <div class="tracking-wide text-muted-foreground">
                        RESOLUTION
                      </div>
                      <div class="text-foreground">
                        {cleanResolutionString(
                          version.version_video_resolution,
                        )}
                      </div>
                    </div>
                  {/if}

                  <!-- size -->
                  {#if version.estimated_space_bytes}
                    <div class="space-y-1 text-xs">
                      <div class="tracking-wide text-muted-foreground">
                        SIZE
                      </div>
                      <div class="text-foreground">
                        {formatFileSize(version.estimated_space_bytes)}
                      </div>
                    </div>
                  {/if}

                  <!-- library -->
                  {#if version.version_library_name}
                    <div class="space-y-1 text-xs">
                      <div class="tracking-wide text-muted-foreground">
                        LIBRARY
                      </div>
                      <div class="text-foreground">
                        {version.version_library_name}
                      </div>
                    </div>
                  {/if}

                  <!-- matched rules -->
                  {#if previewRules.length > 0}
                    <div class="space-y-1">
                      <div
                        class="text-[11px] uppercase tracking-wide text-muted-foreground"
                      >
                        Matched rules
                      </div>
                      <div class="flex flex-wrap gap-1.5">
                        {#each previewRules as rule}
                          <Badge class="border-primary" variant="secondary"
                            >{rule}</Badge
                          >
                        {/each}
                      </div>
                    </div>
                  {/if}

                  <!-- {#if reasons.length > 0}
                  <div class="text-xs text-muted-foreground leading-5">
                    {reasons[0]}
                  </div>
                {/if} -->

                  <div class="flex justify-end gap-2">
                    <CandidateActionButtons
                      entry={version}
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

<CandidateVersionInfoDialog
  entry={infoTarget}
  {formatDate}
  onClose={closeInfo}
/>
