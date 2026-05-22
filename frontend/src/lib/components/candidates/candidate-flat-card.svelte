<script lang="ts">
  import type { Snippet } from "svelte";
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import CandidateTmdbMeta from "$lib/components/candidates/candidate-tmdb-meta.svelte";
  import CandidateActionButtons from "$lib/components/candidates/candidate-action-buttons.svelte";
  import type { ReclaimCandidateEntry } from "$lib/types/shared";

  type PosterSize =
    | "92"
    | "154"
    | "185"
    | "342"
    | "500"
    | "780"
    | "original"
    | number;

  interface Props {
    entry: ReclaimCandidateEntry;
    canBulkSelect: boolean;
    canDelete: boolean;
    selectedIds: Set<number>;
    toggleSelect: (id: number) => void;
    openSingleRequest: (entry: ReclaimCandidateEntry) => void;
    openSingleDelete: (entry: ReclaimCandidateEntry) => void;
    openSingleMove: (entry: ReclaimCandidateEntry) => void;
    moveEnabled: boolean;
    formatDate: (value: string) => string;
    summary: Snippet;
    posterSize?: PosterSize;
    tailWindElSize?: string;
  }

  let {
    entry,
    canBulkSelect,
    canDelete,
    selectedIds,
    toggleSelect,
    openSingleRequest,
    openSingleDelete,
    openSingleMove,
    moveEnabled,
    formatDate,
    summary,
    posterSize,
    tailWindElSize,
  }: Props = $props();
</script>

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
          {posterSize}
          {tailWindElSize}
          showMediaType={true}
        />
        <div class="min-w-0">
          <div class="text-sm font-medium text-foreground">
            {entry.media_title}
            {#if entry.media_year}
              <span class="text-muted-foreground">({entry.media_year})</span>
            {/if}
          </div>
          <div class="mt-1">
            <MediaTypeBadge mediaType={entry.media_type} />
          </div>
          <div class="mt-2 flex flex-wrap gap-1.5">
            {@render summary()}
          </div>
          <div class="mt-2 text-xs text-muted-foreground">
            Date Added: {formatDate(entry.created_at)}
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
