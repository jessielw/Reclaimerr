<script lang="ts">
  import Star from "@lucide/svelte/icons/star";
  import Users from "@lucide/svelte/icons/users";
  import Heart from "@lucide/svelte/icons/heart";
  import Flame from "@lucide/svelte/icons/flame";
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import MetacriticSvg from "$lib/components/svgs/metacritic-svg.svelte";
  import LetterboxSvg from "$lib/components/svgs/letterboxd-svg.svelte";
  import RottenTomatoesPopcornSvg from "$lib/components/svgs/rotten-tomato-popcorn-svg.svelte";
  import RottenTomatoesSvg from "$lib/components/svgs/rotten-tomato-svg.svelte";
  import TraktSvg from "$lib/components/svgs/trakt-svg.svelte";
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
    anilistId?: number | null;
    anilistScore?: number | null;
    anilistPopularity?: number | null;
    anilistFavourites?: number | null;
    rottenTomatoesTomatoMeter?: number | null;
    rottenTomatoesTomatoVoteCount?: number | null;
    rottenTomatoesPopcornMeter?: number | null;
    rottenTomatoesPopcornVoteCount?: number | null;
    metacriticMetascore?: number | null;
    metacriticVoteCount?: number | null;
    metacriticUserScore?: number | null;
    metacriticUserVoteCount?: number | null;
    traktRating?: number | null;
    traktVoteCount?: number | null;
    letterboxdScore?: number | null;
    letterboxdVoteCount?: number | null;
    externalRatingsSource?: string | null;
    variant?: "grid" | "summary";
    compact?: boolean;
    class?: string;
  }

  type SourceIcon =
    | "tmdb"
    | "imdb"
    | "tomatometer"
    | "popcornmeter"
    | "metacritic"
    | "trakt"
    | "letterboxd"
    | "anilist";

  type MetricIcon = "star" | "users" | "heart" | "flame" | "status";
  type MetricTone = "rating" | "count" | "popular" | "favorite" | "status";

  interface Metric {
    key: string;
    value: string;
    tooltip: string;
    icon: MetricIcon;
    tone: MetricTone;
  }

  interface SourceCard {
    key: string;
    label: string;
    summaryLabel: string;
    labelClass: string;
    icon: SourceIcon;
    metrics: Metric[];
    summaryMetricKey?: string;
    sourceLabel?: string | null;
    linkHref?: string;
    linkLabel?: string;
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
    anilistId = null,
    anilistScore = null,
    anilistPopularity = null,
    anilistFavourites = null,
    rottenTomatoesTomatoMeter = null,
    rottenTomatoesTomatoVoteCount = null,
    rottenTomatoesPopcornMeter = null,
    rottenTomatoesPopcornVoteCount = null,
    metacriticMetascore = null,
    metacriticVoteCount = null,
    metacriticUserScore = null,
    metacriticUserVoteCount = null,
    traktRating = null,
    traktVoteCount = null,
    letterboxdScore = null,
    letterboxdVoteCount = null,
    externalRatingsSource = null,
    variant = "grid",
    compact = false,
    class: className = "",
  }: Props = $props();

  const formatCompactVotes = (value: number | null): string | null => {
    if (value == null) return null;
    if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
    return String(value);
  };

  const pushMetric = (
    metrics: Metric[],
    key: string,
    value: string | null,
    tooltip: string,
    icon: MetricIcon,
    tone: MetricTone,
  ) => {
    if (value == null) return;
    metrics.push({ key, value, tooltip, icon, tone });
  };

  const externalSourceTooltip = (label: string, sourceLabel: string | null) =>
    sourceLabel ? `${label} via ${sourceLabel}` : label;

  const getSummaryMetric = (card: SourceCard): Metric | null =>
    card.metrics.find((metric) => metric.key === card.summaryMetricKey) ??
    card.metrics.find((metric) => metric.tone === "rating") ??
    card.metrics[0] ??
    null;

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
  const anilistScoreFmt = $derived.by(() =>
    anilistScore != null ? `${Math.round(anilistScore)}%` : null,
  );
  const anilistPopularityFmt = $derived(formatCompactVotes(anilistPopularity));
  const anilistFavouritesFmt = $derived(formatCompactVotes(anilistFavourites));
  const rottenTomatoesTomatoMeterFmt = $derived.by(() =>
    rottenTomatoesTomatoMeter != null
      ? `${Math.round(rottenTomatoesTomatoMeter)}%`
      : null,
  );
  const rottenTomatoesPopcornMeterFmt = $derived.by(() =>
    rottenTomatoesPopcornMeter != null
      ? `${Math.round(rottenTomatoesPopcornMeter)}%`
      : null,
  );
  const rottenTomatoesTomatoVotesFmt = $derived(
    formatCompactVotes(rottenTomatoesTomatoVoteCount),
  );
  const rottenTomatoesPopcornVotesFmt = $derived(
    formatCompactVotes(rottenTomatoesPopcornVoteCount),
  );
  const metacriticMetascoreFmt = $derived.by(() =>
    metacriticMetascore != null
      ? `${Math.round(metacriticMetascore)}/100`
      : null,
  );
  const metacriticVotesFmt = $derived(formatCompactVotes(metacriticVoteCount));
  const metacriticUserScoreFmt = $derived.by(() =>
    metacriticUserScore != null
      ? `${Math.round(metacriticUserScore)}/100`
      : null,
  );
  const metacriticUserVotesFmt = $derived(
    formatCompactVotes(metacriticUserVoteCount),
  );
  const traktRatingFmt = $derived.by(() =>
    traktRating != null ? `${Math.round(traktRating)}%` : null,
  );
  const traktVotesFmt = $derived(formatCompactVotes(traktVoteCount));
  const letterboxdScoreFmt = $derived.by(() =>
    letterboxdScore != null ? `${Math.round(letterboxdScore)}%` : null,
  );
  const letterboxdVotesFmt = $derived(formatCompactVotes(letterboxdVoteCount));
  const sourceIconClass = $derived(compact ? "size-4" : "size-5");
  const textIconClass = $derived(
    compact ? "text-[10px] font-bold" : "text-[11px] font-bold",
  );
  const iconFrameClass = $derived(
    compact
      ? "flex size-7 shrink-0 items-center justify-center rounded-md border border-border/50 bg-background/70"
      : "flex size-8 shrink-0 items-center justify-center rounded-lg border border-border/50 bg-background/70",
  );
  const cardClass = $derived(
    compact
      ? "rounded-md border border-border/60 bg-muted/35 px-2 py-1.5 text-foreground/90 shadow-sm"
      : "rounded-lg border border-border/60 bg-muted/35 px-3 py-2.5 text-foreground/90 shadow-sm",
  );
  const metricBaseClass = $derived(
    compact
      ? "inline-flex items-center gap-1 rounded-full border px-1.5 py-px text-[11px] cursor-help"
      : "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs cursor-help",
  );
  const summaryPillClass = $derived(
    compact
      ? "inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-[11px] cursor-help"
      : "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs cursor-help",
  );
  const metricIconClass = $derived(compact ? "size-3" : "size-3.5");
  const summarySourceIconClass = $derived(compact ? "size-3.5" : "size-4");
  const externalRatingsSourceLabel = $derived.by(() => {
    if (!externalRatingsSource || externalRatingsSource === "none") return null;
    return externalRatingsSource
      .split("+")
      .map((source) =>
        source === "mdblist" ? "MDBList" : source === "omdb" ? "OMDb" : source,
      )
      .join(" + ");
  });

  const metricToneClass = (tone: MetricTone): string => {
    switch (tone) {
      case "rating":
        return "border-yellow-400/40 bg-yellow-400/15 text-yellow-600";
      case "popular":
        return "border-orange-400/30 bg-orange-400/10 text-orange-600";
      case "favorite":
        return "border-pink-400/30 bg-pink-400/10 text-pink-600";
      case "status":
        return "border-primary/30 bg-primary/10 text-primary";
      case "count":
      default:
        return "border-border/60 bg-background/70 text-muted-foreground";
    }
  };

  const sourceCards = $derived.by((): SourceCard[] => {
    const cards: SourceCard[] = [];

    const tmdbMetrics: Metric[] = [];
    pushMetric(
      tmdbMetrics,
      "rating",
      tmdbRatingFmt,
      "TMDB rating",
      "star",
      "rating",
    );
    pushMetric(
      tmdbMetrics,
      "votes",
      tmdbVotesFmt,
      "TMDB vote count",
      "users",
      "count",
    );
    if (showTmdbPopularity) {
      pushMetric(
        tmdbMetrics,
        "popularity",
        tmdbPopularityFmt,
        "TMDB popularity",
        "flame",
        "popular",
      );
    }
    if (showTmdbStatus) {
      pushMetric(
        tmdbMetrics,
        "status",
        tmdbStatus,
        "TMDB status",
        "status",
        "status",
      );
    }
    if (tmdbMetrics.length) {
      cards.push({
        key: "tmdb",
        label: "TMDB",
        summaryLabel: "TMDB",
        labelClass: "text-tmdb",
        icon: "tmdb",
        metrics: tmdbMetrics,
        summaryMetricKey: "rating",
        linkHref: tmdbId
          ? `https://www.themoviedb.org/${tmdbMediaType}/${tmdbId}`
          : undefined,
        linkLabel: "Open TMDB page",
      });
    }

    const imdbMetrics: Metric[] = [];
    pushMetric(
      imdbMetrics,
      "rating",
      imdbRatingFmt,
      "IMDb rating",
      "star",
      "rating",
    );
    pushMetric(
      imdbMetrics,
      "votes",
      imdbVotesFmt,
      "IMDb vote count",
      "users",
      "count",
    );
    if (imdbMetrics.length) {
      cards.push({
        key: "imdb",
        label: "IMDb",
        summaryLabel: "IMDb",
        labelClass: "text-imdb",
        icon: "imdb",
        metrics: imdbMetrics,
        summaryMetricKey: "rating",
        linkHref: imdbId ? `https://www.imdb.com/title/${imdbId}` : undefined,
        linkLabel: "Open IMDb page",
      });
    }

    const tomatometerMetrics: Metric[] = [];
    pushMetric(
      tomatometerMetrics,
      "rating",
      rottenTomatoesTomatoMeterFmt,
      externalSourceTooltip(
        "Rotten Tomatoes Tomatometer",
        externalRatingsSourceLabel,
      ),
      "star",
      "rating",
    );
    pushMetric(
      tomatometerMetrics,
      "votes",
      rottenTomatoesTomatoVotesFmt,
      "Tomatometer review count",
      "users",
      "count",
    );
    if (tomatometerMetrics.length) {
      cards.push({
        key: "tomatometer",
        label: "Tomatometer",
        summaryLabel: "RT",
        labelClass: "text-red-500",
        icon: "tomatometer",
        metrics: tomatometerMetrics,
        summaryMetricKey: "rating",
        sourceLabel: externalRatingsSourceLabel,
      });
    }

    const popcornMetrics: Metric[] = [];
    pushMetric(
      popcornMetrics,
      "rating",
      rottenTomatoesPopcornMeterFmt,
      externalSourceTooltip(
        "Rotten Tomatoes Popcornmeter",
        externalRatingsSourceLabel,
      ),
      "star",
      "rating",
    );
    pushMetric(
      popcornMetrics,
      "votes",
      rottenTomatoesPopcornVotesFmt,
      "Popcornmeter vote count",
      "users",
      "count",
    );
    if (popcornMetrics.length) {
      cards.push({
        key: "popcornmeter",
        label: "Popcornmeter",
        summaryLabel: "Popcorn",
        labelClass: "text-orange-500",
        icon: "popcornmeter",
        metrics: popcornMetrics,
        summaryMetricKey: "rating",
        sourceLabel: externalRatingsSourceLabel,
      });
    }

    const metacriticMetrics: Metric[] = [];
    pushMetric(
      metacriticMetrics,
      "metascore",
      metacriticMetascoreFmt,
      externalSourceTooltip("Metacritic Metascore", externalRatingsSourceLabel),
      "star",
      "rating",
    );
    pushMetric(
      metacriticMetrics,
      "critic-votes",
      metacriticVotesFmt,
      "Metacritic critic count",
      "users",
      "count",
    );
    pushMetric(
      metacriticMetrics,
      "user-score",
      metacriticUserScoreFmt,
      externalSourceTooltip(
        "Metacritic user score",
        externalRatingsSourceLabel,
      ),
      "star",
      "rating",
    );
    pushMetric(
      metacriticMetrics,
      "user-votes",
      metacriticUserVotesFmt,
      "Metacritic user votes",
      "users",
      "count",
    );
    if (metacriticMetrics.length) {
      cards.push({
        key: "metacritic",
        label: "Metacritic",
        summaryLabel: "MC",
        labelClass: "text-emerald-500",
        icon: "metacritic",
        metrics: metacriticMetrics,
        summaryMetricKey: "metascore",
        sourceLabel: externalRatingsSourceLabel,
      });
    }

    const traktMetrics: Metric[] = [];
    pushMetric(
      traktMetrics,
      "rating",
      traktRatingFmt,
      externalSourceTooltip("Trakt rating", externalRatingsSourceLabel),
      "star",
      "rating",
    );
    pushMetric(
      traktMetrics,
      "votes",
      traktVotesFmt,
      "Trakt votes",
      "users",
      "count",
    );
    if (traktMetrics.length) {
      cards.push({
        key: "trakt",
        label: "Trakt",
        summaryLabel: "Trakt",
        labelClass: "text-red-500",
        icon: "trakt",
        metrics: traktMetrics,
        summaryMetricKey: "rating",
        sourceLabel: externalRatingsSourceLabel,
      });
    }

    const letterboxdMetrics: Metric[] = [];
    pushMetric(
      letterboxdMetrics,
      "score",
      letterboxdScoreFmt,
      externalSourceTooltip("Letterboxd score", externalRatingsSourceLabel),
      "star",
      "rating",
    );
    pushMetric(
      letterboxdMetrics,
      "votes",
      letterboxdVotesFmt,
      "Letterboxd votes",
      "users",
      "count",
    );
    if (letterboxdMetrics.length) {
      cards.push({
        key: "letterboxd",
        label: "Letterboxd",
        summaryLabel: "LB",
        labelClass: "text-orange-500",
        icon: "letterboxd",
        metrics: letterboxdMetrics,
        summaryMetricKey: "score",
        sourceLabel: externalRatingsSourceLabel,
      });
    }

    const anilistMetrics: Metric[] = [];
    pushMetric(
      anilistMetrics,
      "score",
      anilistScoreFmt,
      "AniList score",
      "star",
      "rating",
    );
    pushMetric(
      anilistMetrics,
      "popularity",
      anilistPopularityFmt,
      "AniList popularity",
      "users",
      "count",
    );
    pushMetric(
      anilistMetrics,
      "favourites",
      anilistFavouritesFmt,
      "AniList favourites",
      "heart",
      "favorite",
    );
    if (anilistMetrics.length) {
      cards.push({
        key: "anilist",
        label: "AniList",
        summaryLabel: "AniList",
        labelClass: "text-primary",
        icon: "anilist",
        metrics: anilistMetrics,
        summaryMetricKey: "score",
        linkHref: anilistId
          ? `https://anilist.co/anime/${anilistId}`
          : undefined,
        linkLabel: "Open AniList page",
      });
    }

    return cards;
  });
</script>

{#if sourceCards.length}
  {#if variant === "summary"}
    <div class={`metadata-source-summary ${className}`}>
      {#each sourceCards as card (card.key)}
        {@const metric = getSummaryMetric(card)}
        {#if metric}
          <Tooltip.Root>
            <Tooltip.Trigger class="inline-flex max-w-fit cursor-help">
              {#snippet child({ props })}
                {#if card.linkHref}
                  <a
                    {...props}
                    href={card.linkHref}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={card.linkLabel}
                    tabindex="-1"
                    class={`${summaryPillClass} ${metricToneClass(metric.tone)} hover:border-primary/50 hover:bg-primary/10 hover:text-primary`}
                  >
                    {#if card.icon === "tomatometer"}
                      <RottenTomatoesSvg
                        class={`${summarySourceIconClass} shrink-0`}
                      />
                    {:else if card.icon === "popcornmeter"}
                      <RottenTomatoesPopcornSvg
                        class={`${summarySourceIconClass} shrink-0`}
                      />
                    {:else if card.icon === "metacritic"}
                      <MetacriticSvg
                        class={`${summarySourceIconClass} shrink-0`}
                      />
                    {:else if card.icon === "trakt"}
                      <TraktSvg class={`${summarySourceIconClass} shrink-0`} />
                    {:else if card.icon === "letterboxd"}
                      <LetterboxSvg
                        class={`${summarySourceIconClass} shrink-0`}
                      />
                    {/if}

                    <span class={`font-semibold ${card.labelClass}`}>
                      {card.summaryLabel}
                    </span>
                    <span class="font-medium text-foreground/90">
                      {metric.value}
                    </span>
                    <ExternalLink class={compact ? "size-3" : "size-3.5"} />
                  </a>
                {:else}
                  <span
                    {...props}
                    class={`${summaryPillClass} ${metricToneClass(metric.tone)}`}
                    tabindex="-1"
                  >
                    {#if card.icon === "tomatometer"}
                      <RottenTomatoesSvg
                        class={`${summarySourceIconClass} shrink-0`}
                      />
                    {:else if card.icon === "popcornmeter"}
                      <RottenTomatoesPopcornSvg
                        class={`${summarySourceIconClass} shrink-0`}
                      />
                    {:else if card.icon === "metacritic"}
                      <MetacriticSvg
                        class={`${summarySourceIconClass} shrink-0`}
                      />
                    {:else if card.icon === "trakt"}
                      <TraktSvg class={`${summarySourceIconClass} shrink-0`} />
                    {:else if card.icon === "letterboxd"}
                      <LetterboxSvg
                        class={`${summarySourceIconClass} shrink-0`}
                      />
                    {/if}

                    <span class={`font-semibold ${card.labelClass}`}>
                      {card.summaryLabel}
                    </span>
                    <span class="font-medium text-foreground/90">
                      {metric.value}
                    </span>
                  </span>
                {/if}
              {/snippet}
            </Tooltip.Trigger>
            <Tooltip.Content>
              <div class="space-y-1">
                {#each card.metrics as detail (detail.key)}
                  <p>{detail.tooltip}: {detail.value}</p>
                {/each}
              </div>
            </Tooltip.Content>
          </Tooltip.Root>
        {/if}
      {/each}
    </div>
  {:else}
    <div
      class={`metadata-source-grid ${compact ? "metadata-source-grid--compact" : ""} ${className}`}
    >
      {#each sourceCards as card (card.key)}
        <div class={cardClass}>
          <div class="flex items-start justify-between gap-2">
            <div class="flex min-w-0 items-center gap-2">
              <span class={iconFrameClass} aria-hidden="true">
                {#if card.icon === "tomatometer"}
                  <RottenTomatoesSvg class={`${sourceIconClass} shrink-0`} />
                {:else if card.icon === "popcornmeter"}
                  <RottenTomatoesPopcornSvg
                    class={`${sourceIconClass} shrink-0`}
                  />
                {:else if card.icon === "metacritic"}
                  <MetacriticSvg class={`${sourceIconClass} shrink-0`} />
                {:else if card.icon === "trakt"}
                  <TraktSvg class={`${sourceIconClass} shrink-0`} />
                {:else if card.icon === "letterboxd"}
                  <LetterboxSvg class={`${sourceIconClass} shrink-0`} />
                {:else if card.icon === "anilist"}
                  <span class={`${textIconClass} ${card.labelClass}`}>AL</span>
                {:else if card.icon === "imdb"}
                  <span class={`${textIconClass} ${card.labelClass}`}>IMDb</span
                  >
                {:else}
                  <span class={`${textIconClass} ${card.labelClass}`}>TM</span>
                {/if}
              </span>

              <div class="min-w-0">
                <p
                  class={`${compact ? "text-[11px]" : "text-xs"} truncate font-semibold tracking-wide ${card.labelClass}`}
                >
                  {card.label}
                </p>
                {#if card.sourceLabel}
                  <p
                    class="truncate text-[10px] leading-tight text-muted-foreground"
                  >
                    via {card.sourceLabel}
                  </p>
                {/if}
              </div>
            </div>

            {#if card.linkHref}
              <a
                href={card.linkHref}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={card.linkLabel}
                class={`${compact ? "size-6" : "size-7"} inline-flex shrink-0 items-center justify-center rounded-full text-muted-foreground hover:bg-background hover:text-primary`}
              >
                <ExternalLink class={compact ? "size-3.5" : "size-4"} />
              </a>
            {/if}
          </div>

          <div class={`${compact ? "mt-1.5" : "mt-2"} flex flex-wrap gap-1.5`}>
            {#each card.metrics as metric (metric.key)}
              <Tooltip.Root>
                <Tooltip.Trigger class="inline-flex max-w-fit cursor-help">
                  {#snippet child({ props })}
                    <span
                      {...props}
                      class={`${metricBaseClass} ${metricToneClass(metric.tone)}`}
                      tabindex="-1"
                    >
                      {#if metric.icon === "star"}
                        <Star
                          class={`${metricIconClass} fill-yellow-400 text-yellow-400`}
                        />
                      {:else if metric.icon === "users"}
                        <Users class={metricIconClass} />
                      {:else if metric.icon === "heart"}
                        <Heart
                          class={`${metricIconClass} fill-pink-400 text-pink-500`}
                        />
                      {:else if metric.icon === "flame"}
                        <Flame
                          class={`${metricIconClass} fill-orange-400 text-orange-700`}
                        />
                      {:else}
                        <span
                          class="size-1.5 rounded-full bg-current opacity-70"
                        ></span>
                      {/if}
                      {metric.value}
                    </span>
                  {/snippet}
                </Tooltip.Trigger>
                <Tooltip.Content><p>{metric.tooltip}</p></Tooltip.Content>
              </Tooltip.Root>
            {/each}
          </div>
        </div>
      {/each}
    </div>
  {/if}
{/if}

<style>
  .metadata-source-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    width: 100%;
  }

  .metadata-source-grid {
    --metadata-card-min: 12rem;
    display: grid;
    grid-template-columns: repeat(
      auto-fit,
      minmax(min(100%, var(--metadata-card-min)), 1fr)
    );
    gap: 0.5rem;
    width: 100%;
  }

  .metadata-source-grid--compact {
    --metadata-card-min: 9.75rem;
    gap: 0.375rem;
  }
</style>
