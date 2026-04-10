<script lang="ts">
  import Film from "@lucide/svelte/icons/film";
  import Tv from "@lucide/svelte/icons/tv";
  import { MediaType } from "$lib/types/shared";

  let {
    mediaType,
    posterUrl,
    width = 92,
  }: {
    mediaType: MediaType;
    posterUrl?: string | null;
    width?: number;
  } = $props();

  // ensure posterUrl is normalized to start with a slash if it exists
  let normalizedPosterUrl = $derived.by(() => {
    if (!posterUrl) return null;
    return posterUrl.startsWith("/") ? posterUrl : `/${posterUrl}`;
  });
</script>

<div
  class="w-16 shrink-0 rounded-md bg-muted/40 flex items-center justify-center aspect-2/3 border-2
    transition-transform duration-150 border-gray-400 dark:border-gray-700 hover:scale-105
    cursor-default"
>
  {#if normalizedPosterUrl}
    <img
      src={`http://image.tmdb.org/t/p/w${width}${normalizedPosterUrl}`}
      alt="Poster"
      class="h-full w-full object-cover rounded-md"
    />
  {:else if mediaType === MediaType.Movie}
    <Film class="w-4 h-4 text-muted-foreground" />
  {:else}
    <Tv class="w-4 h-4 text-muted-foreground" />
  {/if}
</div>
