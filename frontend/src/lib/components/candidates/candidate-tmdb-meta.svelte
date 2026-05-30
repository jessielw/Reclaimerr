<script lang="ts">
  import MetadataSources from "$lib/components/media/metadata-sources.svelte";
  import type { ReclaimCandidateEntry } from "$lib/types/shared";

  interface Props {
    entry: ReclaimCandidateEntry;
  }

  let { entry }: Props = $props();

  const genres = $derived((entry.genres ?? []).slice(0, 3));
  const collectionName = $derived(
    entry.media_type === "movie" ? entry.tmdb_collection_name : null,
  );

  const tmdbMediaType = $derived(
    entry.media_type === "series" ? "tv" : "movie",
  );
  const hasAny = $derived(
    entry.vote_average != null ||
      entry.vote_count != null ||
      entry.popularity != null ||
      entry.tmdb_status != null ||
      entry.imdb_rating != null ||
      entry.imdb_vote_count != null ||
      entry.anilist_score != null ||
      entry.anilist_popularity != null ||
      entry.anilist_favourites != null ||
      genres.length > 0 ||
      !!collectionName,
  );
</script>

{#if hasAny}
  <div
    class="mt-2 flex flex-col gap-1.5 cursor-default"
    onclick={(e) => e.stopPropagation()}
    role="none"
  >
    <MetadataSources
      tmdbId={entry.tmdb_id}
      {tmdbMediaType}
      tmdbRating={entry.vote_average}
      tmdbVoteCount={entry.vote_count}
      tmdbPopularity={entry.popularity}
      tmdbStatus={entry.tmdb_status}
      tmdbRatingStyle="percent"
      showTmdbPopularity={true}
      showTmdbStatus={tmdbMediaType === "tv"}
      imdbId={entry.imdb_id}
      imdbRating={entry.imdb_rating}
      imdbVoteCount={entry.imdb_vote_count}
      anilistId={entry.anilist_id}
      anilistScore={entry.anilist_score}
      anilistPopularity={entry.anilist_popularity}
      anilistFavourites={entry.anilist_favourites}
      compact={true}
      class="w-40 md:w-64"
    />

    <!-- genres -->
    {#if genres.length}
      <div class="flex flex-wrap gap-1">
        {#each genres as genre}
          <span
            class="text-xs px-1.5 py-px rounded-full bg-muted border border-border/60 text-muted-foreground"
            >{genre}</span
          >
        {/each}
      </div>
    {/if}

    {#if collectionName}
      <div class="text-xs text-muted-foreground">
        Collection: {collectionName}
      </div>
    {/if}
  </div>
{/if}
