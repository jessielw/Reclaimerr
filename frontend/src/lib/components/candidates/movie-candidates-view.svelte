<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import { MediaType, type ReclaimCandidateEntry } from "$lib/types/shared";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import Info from "@lucide/svelte/icons/info";
  import Shield from "@lucide/svelte/icons/shield";
  import Trash from "@lucide/svelte/icons/trash";
  import X from "@lucide/svelte/icons/x";
  import VersionMediaInfoWidget from "$lib/components/candidates/movie-version-mediainfo.svelte";

  type FlatRow = { kind: "flat"; entry: ReclaimCandidateEntry };
  type MovieGroupRow = {
    kind: "group";
    group_type: "movie_versions";
    seriesEntry: ReclaimCandidateEntry | null;
    seasons: ReclaimCandidateEntry[];
    versions: ReclaimCandidateEntry[];
    media_id: number;
    media_title: string;
    media_year: number | null;
    poster_url: string | null;
  };
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
    formatDate: (value: string) => string;
    sizeLabel: (value: number | null) => string;
    groupTotalGb: (row: MovieGroupRow) => number;
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
    formatDate,
    sizeLabel,
    groupTotalGb,
  }: Props = $props();

  let infoOpen = $state(false);
  let infoTarget = $state<ReclaimCandidateEntry | null>(null);

  const movieSummaryChips = (entry: ReclaimCandidateEntry): string[] => {
    const chips: string[] = [];
    if (entry.version_video_width && entry.version_video_height) {
      chips.push(`${entry.version_video_width}x${entry.version_video_height}`);
    }
    if (entry.version_video_codec_family) {
      chips.push(entry.version_video_codec_family.toUpperCase());
    }
    if (entry.version_video_dolby_vision) chips.push("DV");
    else if (entry.version_video_hdr) chips.push("HDR");
    if (entry.version_audio_codec_family) {
      chips.push(entry.version_audio_codec_family.toUpperCase());
    }
    chips.push(sizeLabel(entry.estimated_space_gb));
    return chips;
  };

  const unknownValue = "Unknown";

  const resolutionLabel = (entry: ReclaimCandidateEntry): string => {
    if (entry.version_video_resolution) {
      return entry.version_video_resolution;
    } else if (entry.version_video_height && entry.version_video_height > 0) {
      return `${entry.version_video_height}p`;
    }
    return unknownValue;
  };

  const fileNameFromPath = (
    path: string | null,
    fallbackFileName: string | null = null,
  ): string => {
    if (fallbackFileName && fallbackFileName.trim()) {
      return fallbackFileName.trim();
    }
    if (!path) return unknownValue;
    const parts = path.split(/[/\\]/);
    const extractedFileName = parts[parts.length - 1];
    return extractedFileName?.trim() ? extractedFileName : unknownValue;
  };

  const parseRuleTokens = (tokens: string[] | null | undefined): string[] =>
    (tokens ?? []).map((token) => token.trim()).filter(Boolean);

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
                  {#each movieSummaryChips(entry) as chip}
                    <span
                      class="text-xs leading-5 px-2 rounded-full border border-border bg-muted/50 text-foreground"
                    >
                      {chip}
                    </span>
                  {/each}
                </div>
                <div class="mt-2 text-xs text-muted-foreground">
                  {formatDate(entry.created_at)}
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="flex justify-end gap-2">
          {#if entry.has_pending_request}
            <span class="text-xs text-blue-400 self-center"
              >Pending request</span
            >
          {:else}
            <Button
              size="icon"
              class="cursor-pointer rounded-full"
              onclick={() => openSingleRequest(entry)}
            >
              <Shield class="size-4" />
            </Button>
          {/if}
          {#if canDelete}
            <Button
              size="icon"
              class="cursor-pointer rounded-full bg-destructive/80 hover:bg-destructive/60"
              onclick={() => openSingleDelete(entry)}
            >
              <Trash class="size-4" />
            </Button>
          {/if}
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
                mediaType={MediaType.Movie}
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
              </div>
            </div>
          </button>
        </div>
        {#if expanded}
          <div class="pl-7 space-y-2">
            {#each row.versions as version (version.id)}
              {@const parsedRules = parseRuleTokens(version.reason_tokens)}
              <div
                class="rounded-md border border-border bg-muted/30 p-3 space-y-2"
              >
                <div class="flex items-start justify-between gap-2">
                  <div class="min-w-0">
                    <div class="text-sm font-medium text-foreground">
                      {version.version_library_name ?? "Version"} - {resolutionLabel(
                        version,
                      )}
                    </div>
                    <div class="text-xs text-muted-foreground truncate">
                      {fileNameFromPath(
                        version.version_path,
                        version.version_file_name,
                      )}
                    </div>
                  </div>
                  <div class="text-xs text-muted-foreground">
                    {formatDate(version.created_at)}
                  </div>
                </div>
                <div class="text-xs text-muted-foreground">
                  {sizeLabel(version.estimated_space_gb)}
                </div>
                <div class="flex flex-wrap gap-1.5">
                  {#if parsedRules.length > 0}
                    {#each parsedRules as rule}
                      <span
                        class="text-xs leading-5 px-2 rounded-full border border-border bg-card text-foreground"
                      >
                        {rule}
                      </span>
                    {/each}
                  {:else}
                    <span class="text-xs text-muted-foreground"
                      >{unknownValue}</span
                    >
                  {/if}
                </div>
                <div class="flex justify-end gap-2">
                  {#if version.has_pending_request}
                    <span class="text-xs text-blue-400 self-center"
                      >Pending request</span
                    >
                  {:else}
                    <Button
                      size="icon"
                      class="cursor-pointer rounded-full size-7"
                      onclick={() => openSingleRequest(version)}
                    >
                      <Shield class="size-3.5" />
                    </Button>
                  {/if}
                  {#if canDelete}
                    <Button
                      size="icon"
                      class="cursor-pointer rounded-full size-7 bg-destructive/80 hover:bg-destructive/60"
                      onclick={() => openSingleDelete(version)}
                    >
                      <Trash class="size-3.5" />
                    </Button>
                  {/if}
                  <Button
                    size="icon"
                    class="cursor-pointer rounded-full size-7"
                    onclick={() => openInfo(version)}
                  >
                    <Info class="size-3.5" />
                  </Button>
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
  {/each}
</div>

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
                </div>
              </div>
            </td>
            <td
              class="px-6 py-3 text-sm text-muted-foreground whitespace-normal wrap-break-word"
            >
              <div class="flex flex-wrap gap-1.5">
                {#each movieSummaryChips(entry) as chip}
                  <span
                    class="text-xs leading-5 px-2 rounded-full border border-border bg-muted/50 text-foreground"
                  >
                    {chip}
                  </span>
                {/each}
              </div>
            </td>
            <td class="px-6 py-4 text-sm text-foreground whitespace-nowrap"
              >{sizeLabel(entry.estimated_space_gb)}</td
            >
            <td
              class="px-6 py-4 text-sm text-muted-foreground whitespace-nowrap"
              >{formatDate(entry.created_at)}</td
            >
            <td class="px-6 py-4 text-right whitespace-nowrap">
              <div class="flex gap-2 justify-end items-center">
                {#if entry.has_pending_request}
                  <span class="text-xs text-blue-400">Pending request</span>
                {:else}
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Button
                        size="icon"
                        class="cursor-pointer rounded-full"
                        onclick={() => openSingleRequest(entry)}
                      >
                        <Shield class="size-4" />
                      </Button>
                    </Tooltip.Trigger>
                    <Tooltip.Content><p>Protect</p></Tooltip.Content>
                  </Tooltip.Root>
                {/if}
                {#if canDelete}
                  <Tooltip.Root>
                    <Tooltip.Trigger>
                      <Button
                        size="icon"
                        class="cursor-pointer rounded-full bg-destructive/80 hover:bg-destructive/60"
                        onclick={() => openSingleDelete(entry)}
                      >
                        <Trash class="size-4" />
                      </Button>
                    </Tooltip.Trigger>
                    <Tooltip.Content><p>Delete</p></Tooltip.Content>
                  </Tooltip.Root>
                {/if}
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
                    mediaType={MediaType.Movie}
                    posterUrl={row.poster_url}
                  />
                  <MediaTypeBadge mediaType={MediaType.Movie} />
                </div>
                <div class="min-w-0 flex-1 text-sm font-medium text-foreground">
                  {row.media_title}
                  {#if row.media_year}
                    <span class="text-muted-foreground">({row.media_year})</span
                    >
                  {/if}
                  <div class="mt-0.5">
                    <span class="text-xs text-amber-400 font-normal">
                      {row.versions.length} version{row.versions.length !== 1
                        ? "s"
                        : ""} flagged
                    </span>
                  </div>
                </div>
              </div>
            </td>
            <td
              class="px-6 py-3 text-sm text-muted-foreground whitespace-normal wrap-break-word"
            >
              <div class="flex flex-wrap gap-1.5">
                {#each movieSummaryChips(row.versions[0]) as chip}
                  <span
                    class="text-xs leading-5 px-2 rounded-full border border-border bg-muted/50 text-foreground"
                  >
                    {chip}
                  </span>
                {/each}
              </div>
            </td>
            <td class="px-6 py-4 text-sm text-foreground whitespace-nowrap"
              >{groupTotalGb(row).toFixed(2)} GB</td
            >
            <td
              class="px-6 py-4 text-sm text-muted-foreground whitespace-nowrap"
              >{formatDate(row.versions[0].created_at)}</td
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
            {#each row.versions as version (version.id)}
              {@const parsedRules = parseRuleTokens(version.reason_tokens)}
              <tr
                class="bg-muted/20 border-l-2 border-l-cyan-500/40 hover:bg-muted/40
                  transition-colors {selectedIds.has(version.id)
                  ? 'bg-primary/5'
                  : ''}"
              >
                {#if canBulkSelect}
                  <td class="px-4 py-3 w-10 pl-8">
                    <input
                      type="checkbox"
                      checked={selectedIds.has(version.id)}
                      onchange={() => toggleSelect(version.id)}
                      class="cursor-pointer accent-primary"
                    />
                  </td>
                {/if}
                <td class="px-6 py-3 pl-14">
                  <div class="min-w-0">
                    <div class="text-sm font-medium text-foreground">
                      {version.version_library_name ?? "Version"} - {resolutionLabel(
                        version,
                      )}
                    </div>
                    <div
                      class="text-xs text-muted-foreground truncate max-w-72"
                    >
                      {fileNameFromPath(
                        version.version_path,
                        version.version_file_name,
                      )}
                    </div>
                  </div>
                </td>
                <td
                  class="px-6 py-3 text-sm text-muted-foreground whitespace-normal wrap-break-word"
                >
                  <div class="flex flex-wrap gap-1.5">
                    {#if parsedRules.length > 0}
                      {#each parsedRules as rule}
                        <span
                          class="text-xs leading-5 px-2 rounded-2xl border border-border
                            bg-muted/50 text-foreground"
                        >
                          {rule}
                        </span>
                      {/each}
                    {:else}
                      <span class="text-xs text-muted-foreground"
                        >{unknownValue}</span
                      >
                    {/if}
                  </div>
                </td>
                <td class="px-6 py-3 text-sm text-foreground whitespace-nowrap"
                  >{sizeLabel(version.estimated_space_gb)}</td
                >
                <td
                  class="px-6 py-3 text-sm text-muted-foreground whitespace-nowrap"
                  >{formatDate(version.created_at)}</td
                >
                <td class="px-6 py-3 text-right whitespace-nowrap">
                  <div class="flex gap-2 justify-end items-center">
                    {#if version.has_pending_request}
                      <span class="text-xs text-blue-400">Pending request</span>
                    {:else}
                      <Tooltip.Root>
                        <Tooltip.Trigger>
                          <Button
                            size="icon"
                            class="cursor-pointer rounded-full size-7"
                            onclick={() => openSingleRequest(version)}
                          >
                            <Shield class="size-3.5" />
                          </Button>
                        </Tooltip.Trigger>
                        <Tooltip.Content><p>Protect</p></Tooltip.Content>
                      </Tooltip.Root>
                    {/if}
                    {#if canDelete}
                      <Tooltip.Root>
                        <Tooltip.Trigger>
                          <Button
                            size="icon"
                            class="cursor-pointer rounded-full size-7 bg-destructive/80 hover:bg-destructive/60"
                            onclick={() => openSingleDelete(version)}
                          >
                            <Trash class="size-3.5" />
                          </Button>
                        </Tooltip.Trigger>
                        <Tooltip.Content><p>Delete</p></Tooltip.Content>
                      </Tooltip.Root>
                    {/if}
                    <Tooltip.Root>
                      <Tooltip.Trigger>
                        <Button
                          size="icon"
                          class="cursor-pointer rounded-full size-7"
                          onclick={() => openInfo(version)}
                        >
                          <Info class="size-3.5" />
                        </Button>
                      </Tooltip.Trigger>
                      <Tooltip.Content><p>Details</p></Tooltip.Content>
                    </Tooltip.Root>
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
            {infoTarget.version_library_name ?? "Version"} - {resolutionLabel(
              infoTarget,
            )}
          </div>
          <div class="text-xs text-muted-foreground truncate">
            {fileNameFromPath(
              infoTarget.version_path,
              infoTarget.version_file_name,
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
        <VersionMediaInfoWidget entry={infoTarget} {formatDate} />
      </div>
    </div>
  </div>
{/if}
