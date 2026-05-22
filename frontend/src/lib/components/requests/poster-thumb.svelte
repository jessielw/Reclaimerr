<script lang="ts">
  import Film from "@lucide/svelte/icons/film";
  import Tv from "@lucide/svelte/icons/tv";
  import { MediaType } from "$lib/types/shared";

  type TMDBPosterSizeOption =
    | "92"
    | "154"
    | "185"
    | "342"
    | "500"
    | "780"
    | "original";

  let {
    mediaType,
    posterUrl,
    posterSize = "92",
    tailWindElSize = "w-16",
    showMediaType = false,
  }: {
    mediaType: MediaType;
    posterUrl?: string | null;
    posterSize?: TMDBPosterSizeOption | number;
    tailWindElSize?: string;
    showMediaType?: boolean;
  } = $props();

  // border based on media type
  const borderColor = $derived.by(() => {
    const defaultColor = "border bg-gray-800 dark:border-gray-700";
    if (!showMediaType) return defaultColor;
    if (mediaType === "movie") return "border-2 border-movie";
    if (mediaType === "series") return "border-2 border-series";
    return defaultColor;
  });

  // ensure posterUrl is normalized to start with a slash if it exists
  let normalizedPosterUrl = $derived.by(() => {
    if (!posterUrl) return null;
    return posterUrl.startsWith("/") ? posterUrl : `/${posterUrl}`;
  });
</script>

<div
  class="{tailWindElSize} shrink-0 self-start rounded-md bg-muted/40 flex items-center justify-center
    aspect-2/3 transition-transform duration-150 {borderColor} hover:scale-105 cursor-default"
>
  {#if normalizedPosterUrl}
    <img
      src={`http://image.tmdb.org/t/p/w${posterSize}${normalizedPosterUrl}`}
      alt="Poster"
      class="h-full w-full object-cover rounded-md"
    />
  {:else if mediaType === MediaType.Movie}
    <Film class="w-4 h-4 text-muted-foreground" />
  {:else}
    <Tv class="w-4 h-4 text-muted-foreground" />
  {/if}
</div>
