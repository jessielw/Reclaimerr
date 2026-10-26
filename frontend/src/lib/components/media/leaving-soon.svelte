<script lang="ts">
  import { push } from "svelte-spa-router";
  import { get_api } from "$lib/api";
  import { auth } from "$lib/stores/auth";
  import { hasPageAccess } from "$lib/page-access";
  import PosterRow, {
    type PosterRowEntry,
  } from "$lib/components/media/poster-row.svelte";
  import { formatDate, formatDistanceToNow } from "$lib/utils/date";
  import { formatFileSize } from "$lib/utils/formatters";
  import {
    MediaType,
    PageAccess,
    type PaginatedResponse,
    type ReclaimCandidateEntry,
  } from "$lib/types/shared";

  type LeavingEntry = PosterRowEntry<ReclaimCandidateEntry>;

  const canViewCandidates = $derived(
    hasPageAccess($auth.user, PageAccess.Candidates),
  );

  const scopeLabel = (c: ReclaimCandidateEntry): string | null => {
    if (c.episode_number != null && c.season_number != null) {
      return `Season ${c.season_number}, Episode ${c.episode_number}`;
    }
    if (c.season_number != null) return `Season ${c.season_number}`;
    return null;
  };

  const toEntry = (
    candidate: ReclaimCandidateEntry,
    moreFromTitle: number,
  ): LeavingEntry => {
    const title = candidate.series_title ?? candidate.media_title;
    const name = candidate.media_year
      ? `${title} (${candidate.media_year})`
      : title;
    const kind = candidate.media_type === MediaType.Movie ? "Movie" : "Series";
    const verb = candidate.delete_operation === "move" ? "Moves" : "Deletes";
    const when = candidate.auto_delete_is_eligible
      ? "now"
      : formatDate(candidate.auto_delete_eligible_at);

    const tooltip = [name];
    const scope = scopeLabel(candidate);
    if (scope) tooltip.push(scope);
    if (moreFromTitle > 0) {
      tooltip.push(`+${moreFromTitle} more scheduled from this title`);
    }
    tooltip.push(`${kind} • ${verb} ${when}`);
    if (candidate.estimated_space_bytes) {
      tooltip.push(`Frees ${formatFileSize(candidate.estimated_space_bytes)}`);
    }

    return {
      key: `${candidate.media_type}:${candidate.media_id}`,
      mediaType: candidate.media_type,
      title,
      subtitle: candidate.auto_delete_is_eligible
        ? "Due now"
        : formatDistanceToNow(candidate.auto_delete_eligible_at),
      posterUrl: candidate.poster_url,
      tooltip,
      data: candidate,
    };
  };

  // the API sorts active deletions soonest first and inactive ones after, so
  // dropping the inactive tail leaves the schedule in order
  const fetchEntries = async (count: number): Promise<LeavingEntry[]> => {
    const params = new URLSearchParams({
      page: "1",
      per_page: count.toString(),
      sort_by: "auto_delete_eligible_at",
      sort_order: "asc",
    });
    const data = await get_api<PaginatedResponse<ReclaimCandidateEntry>>(
      `/api/media/candidates?${params.toString()}`,
    );

    // seasons, episodes and versions of one title share a poster; keep the
    // soonest and count the rest
    const byTitle = new Map<string, ReclaimCandidateEntry[]>();
    for (const candidate of data.items) {
      if (!candidate.auto_delete_is_active) continue;
      const key = `${candidate.media_type}:${candidate.media_id}`;
      byTitle.set(key, [...(byTitle.get(key) ?? []), candidate]);
    }
    return [...byTitle.values()]
      .map(([first, ...rest]) => toEntry(first, rest.length))
      .slice(0, count);
  };
</script>

{#if canViewCandidates}
  <PosterRow
    title="Leaving Soon"
    storageKey="dashboard_leaving_soon"
    emptyText="Nothing scheduled. Titles show up here once a rule with auto-delete schedules them."
    {fetchEntries}
    onSelect={(entry) =>
      push(`/candidates?search=${encodeURIComponent(entry.title)}`)}
  />
{/if}
