<script lang="ts">
  import { get_api } from "$lib/api";
  import { auth } from "$lib/stores/auth";
  import { hasPageAccess } from "$lib/page-access";
  import PosterRow, {
    type PosterRowEntry,
  } from "$lib/components/media/poster-row.svelte";
  import {
    formatDateTimeToLocaleString,
    formatDistanceToNow,
  } from "$lib/utils/date";
  import { formatFileSize } from "$lib/utils/formatters";
  import {
    MediaType,
    PageAccess,
    type DashboardLeavingSoonTitle,
  } from "$lib/types/shared";

  type LeavingEntry = PosterRowEntry<DashboardLeavingSoonTitle>;

  const canViewCandidates = $derived(
    hasPageAccess($auth.user, PageAccess.Candidates),
  );

  const scopeLabel = (item: DashboardLeavingSoonTitle): string | null => {
    if (item.episode_number != null && item.season_number != null) {
      return `Season ${item.season_number}, Episode ${item.episode_number}`;
    }
    if (item.season_number != null) return `Season ${item.season_number}`;
    if (item.movie_version_id != null) return "Movie version";
    return null;
  };

  const toEntry = (item: DashboardLeavingSoonTitle): LeavingEntry => {
    const { title, year, delete_at, media_type, scheduled_deletion_id } = item;
    const name = year ? `${title} (${year})` : title;
    const kind = media_type === MediaType.Movie ? "Movie" : "Series";
    const verb = item.delete_operation === "move" ? "Moves" : "Deletes";
    const due = Date.parse(delete_at) <= Date.now();

    const tooltip = [name];
    const scope = scopeLabel(item);
    if (scope) tooltip.push(scope);
    if (item.source === "manual") tooltip.push("Manually scheduled");
    if (item.source === "delete_request")
      tooltip.push("Approved deletion request");
    tooltip.push(
      `${kind} • ${verb} ${formatDateTimeToLocaleString(delete_at)}`,
    );
    if (item.additional_count > 0) {
      tooltip.push(`+${item.additional_count} more scheduled from this title`);
    }
    if (item.size_bytes) {
      tooltip.push(`Frees ${formatFileSize(item.size_bytes)}`);
    }

    return {
      key: `${media_type}:${item.media_id}`,
      mediaType: media_type,
      title,
      subtitle: due ? "Due now" : formatDistanceToNow(delete_at),
      posterUrl: item.poster_url,
      tooltip,
      href:
        scheduled_deletion_id != null
          ? `#/candidates?schedule=${scheduled_deletion_id}`
          : `#/candidates?search=${encodeURIComponent(title)}`,
      data: item,
    };
  };

  const fetchEntries = async (count: number): Promise<LeavingEntry[]> => {
    const items = await get_api<DashboardLeavingSoonTitle[]>(
      `/api/dashboard/leaving-soon?limit=${count}`,
    );
    return items.map(toEntry);
  };
</script>

{#if canViewCandidates}
  <PosterRow
    title="Leaving Soon"
    storageKey="dashboard_leaving_soon"
    emptyText="No upcoming deletions or moves."
    {fetchEntries}
  />
{/if}
