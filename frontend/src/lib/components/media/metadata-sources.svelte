<script lang="ts">
  import Star from "@lucide/svelte/icons/star";
  import Users from "@lucide/svelte/icons/users";
  import Flame from "@lucide/svelte/icons/flame";
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";

  interface Props {
    tmdbId?: number | null;
    tmdbMediaType?: "movie" | "tv";
    tmdbRating?: number | null;
    tmdbVoteCount?: number | null;
    tmdbPopularity?: number | null;
    tmdbStatus?: string | null;
    tmdbRatingStyle?: "percent" | "score";
    showTmdbPopularity?: boolean;
    showTmdbStatus?: boolean;
    imdbId?: string | null;
    imdbRating?: number | null;
    imdbVoteCount?: number | null;
    compact?: boolean;
    class?: string;
  }

  let {
    tmdbId = null,
    tmdbMediaType = "movie",
    tmdbRating = null,
    tmdbVoteCount = null,
    tmdbPopularity = null,
    tmdbStatus = null,
    tmdbRatingStyle = "score",
    showTmdbPopularity = false,
    showTmdbStatus = false,
    imdbId = null,
    imdbRating = null,
    imdbVoteCount = null,
    compact = false,
    class: className = "",
  }: Props = $props();

  const formatCompactVotes = (value: number | null): string | null => {
    if (value == null) return null;
    if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
    return String(value);
  };

  const tmdbRatingFmt = $derived.by(() => {
    if (tmdbRating == null) return null;
    if (tmdbRatingStyle === "percent") return `${Math.round(tmdbRating * 10)}%`;
    return `${tmdbRating.toFixed(1)}/10`;
  });
  const tmdbVotesFmt = $derived(formatCompactVotes(tmdbVoteCount));
  const tmdbPopularityFmt = $derived.by(() =>
    tmdbPopularity != null ? String(Math.round(tmdbPopularity)) : null,
  );

  const imdbRatingFmt = $derived.by(() =>
    imdbRating != null ? `${imdbRating.toFixed(1)}/10` : null,
  );
  const imdbVotesFmt = $derived(formatCompactVotes(imdbVoteCount));

  const hasTmdbMeta = $derived(
    tmdbRatingFmt != null ||
      tmdbVotesFmt != null ||
      (showTmdbPopularity && tmdbPopularityFmt != null) ||
      (showTmdbStatus && tmdbStatus != null),
  );
  const hasImdbMeta = $derived(imdbRatingFmt != null || imdbVotesFmt != null);
</script>

{#if hasTmdbMeta || hasImdbMeta}
  <div
    class={`rounded-md border border-border/60 bg-muted/40 text-foreground/90 divide-y divide-border/60 ${className}`}
  >
    {#if hasTmdbMeta}
      <div class="flex items-center gap-2 px-2 py-1.5">
        <span
          class={`${compact ? "text-[11px] min-w-12" : "text-xs min-w-14"} font-semibold tracking-wide 
            text-tmdb`}>TMDB</span
        >

        <div class="flex flex-1 flex-wrap items-center gap-x-3 gap-y-1">
          {#if tmdbRatingFmt}
            <Tooltip.Root>
              <Tooltip.Trigger class="inline-flex max-w-fit cursor-help">
                <span class="inline-flex items-center gap-1 text-xs">
                  <Star
                    class={`${compact ? "size-3" : "size-3.5"} fill-yellow-400 text-yellow-400`}
                  />
                  {tmdbRatingFmt}
                </span>
              </Tooltip.Trigger>
              <Tooltip.Content><p>TMDB rating</p></Tooltip.Content>
            </Tooltip.Root>
          {/if}

          {#if tmdbVotesFmt}
            <Tooltip.Root>
              <Tooltip.Trigger class="inline-flex max-w-fit cursor-help">
                <span class="inline-flex items-center gap-1 text-xs">
                  <Users class={`${compact ? "size-3" : "size-3.5"}`} />
                  {tmdbVotesFmt}
                </span>
              </Tooltip.Trigger>
              <Tooltip.Content><p>TMDB vote count</p></Tooltip.Content>
            </Tooltip.Root>
          {/if}

          {#if showTmdbPopularity && tmdbPopularityFmt}
            <Tooltip.Root>
              <Tooltip.Trigger class="inline-flex max-w-fit cursor-help">
                <span class="inline-flex items-center gap-0.5 text-xs">
                  <Flame
                    class={`${compact ? "size-3" : "size-3.5"} fill-orange-400 text-orange-700`}
                  />
                  {tmdbPopularityFmt}
                </span>
              </Tooltip.Trigger>
              <Tooltip.Content><p>TMDB popularity</p></Tooltip.Content>
            </Tooltip.Root>
          {/if}
        </div>

        {#if tmdbId}
          <a
            href={`https://www.themoviedb.org/${tmdbMediaType}/${tmdbId}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            <ExternalLink
              class={`${compact ? "size-3.5" : "size-4"} opacity-75 hover:opacity-100 hover:text-primary`}
            />
          </a>
        {/if}
      </div>
    {/if}

    {#if hasImdbMeta}
      <div class="flex items-center gap-2 px-2 py-1.5">
        <span
          class={`${compact ? "text-[11px] min-w-12" : "text-xs min-w-14"} font-semibold tracking-wide text-(--color-imdb)`}
          >IMDb</span
        >

        <div class="flex flex-1 flex-wrap items-center gap-x-3 gap-y-1">
          {#if imdbRatingFmt}
            <Tooltip.Root>
              <Tooltip.Trigger class="inline-flex max-w-fit cursor-help">
                <span class="inline-flex items-center gap-1 text-xs">
                  <Star
                    class={`${compact ? "size-3" : "size-3.5"} fill-yellow-400 text-yellow-400`}
                  />
                  {imdbRatingFmt}
                </span>
              </Tooltip.Trigger>
              <Tooltip.Content><p>IMDb rating</p></Tooltip.Content>
            </Tooltip.Root>
          {/if}

          {#if imdbVotesFmt}
            <Tooltip.Root>
              <Tooltip.Trigger class="inline-flex max-w-fit cursor-help">
                <span class="inline-flex items-center gap-1 text-xs">
                  <Users class={`${compact ? "size-3" : "size-3.5"}`} />
                  {imdbVotesFmt}
                </span>
              </Tooltip.Trigger>
              <Tooltip.Content><p>IMDb vote count</p></Tooltip.Content>
            </Tooltip.Root>
          {/if}
        </div>

        {#if imdbId}
          <a
            href={`https://www.imdb.com/title/${imdbId}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            <ExternalLink
              class={`${compact ? "size-3.5" : "size-4"} opacity-75 hover:opacity-100 hover:text-primary`}
            />
          </a>
        {/if}
      </div>
    {/if}
  </div>
{/if}
