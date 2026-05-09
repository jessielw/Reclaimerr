<script lang="ts">
  import TmdbSvg from "$lib/components/svgs/tmdb-svg.svelte";
  import Star from "@lucide/svelte/icons/star";
  import Users from "@lucide/svelte/icons/users";
  import Flame from "@lucide/svelte/icons/flame";
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import type { ReclaimCandidateEntry } from "$lib/types/shared";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";

  interface Props {
    entry: ReclaimCandidateEntry;
  }

  let { entry }: Props = $props();

  const hasAny = $derived(
    entry.vote_average != null ||
      entry.vote_count != null ||
      entry.popularity != null ||
      (entry.genres?.length ?? 0) > 0 ||
      entry.tmdb_status != null,
  );

  const votePct = $derived(
    entry.vote_average != null
      ? `${Math.round(entry.vote_average * 10)}%`
      : null,
  );

  const voteCountFmt = $derived(
    entry.vote_count != null
      ? entry.vote_count >= 1000
        ? `${(entry.vote_count / 1000).toFixed(1)}k`
        : String(entry.vote_count)
      : null,
  );

  const popularityFmt = $derived(
    entry.popularity != null ? String(Math.round(entry.popularity)) : null,
  );

  const genres = $derived((entry.genres ?? []).slice(0, 3));

  const mediaType = $derived(entry.media_type === "series" ? "tv" : "movie");
</script>

{#if hasAny}
  <div
    class="mt-2 flex flex-col gap-1.5 cursor-default"
    onclick={(e) => e.stopPropagation()}
    role="none"
  >
    {#if votePct || voteCountFmt || popularityFmt}
      <div
        class="flex flex-col gap-1 relative rounded-md bg-muted/50 border border-border/50 px-2 py-1"
      >
        <!-- tmdb icon -->
        <span class="inline-flex max-w-fit items-center">
          <TmdbSvg class="size-2.5 w-auto" />
        </span>

        <!-- external link positioned at the top right -->
        {#if entry.tmdb_id}
          <a
            class="absolute top-1 right-1"
            href={`https://www.themoviedb.org/${mediaType}/${entry.tmdb_id}`}
            target="_blank"
            rel="noopener noreferrer"
            onclick={(e) => e.stopPropagation()}
          >
            <ExternalLink
              class="size-4.5 opacity-75 hover:opacity-100 hover:text-primary"
            />
          </a>
        {/if}

        <!-- vote percentage -->
        {#if votePct}
          <Tooltip.Root>
            <Tooltip.Trigger class="inline-flex max-w-fit cursor-help">
              <span class="inline-flex items-center gap-1 text-xs">
                <Star class="size-3 fill-yellow-400 text-yellow-400" />
                {votePct}
              </span>
            </Tooltip.Trigger>
            <Tooltip.Content><p>Vote percentage</p></Tooltip.Content>
          </Tooltip.Root>
        {/if}

        <!-- vote count -->
        {#if voteCountFmt}
          <Tooltip.Root>
            <Tooltip.Trigger class="inline-flex max-w-fit cursor-help">
              <span class="inline-flex items-center gap-1 text-xs">
                <Users class="size-3" />
                {voteCountFmt}
              </span>
            </Tooltip.Trigger>
            <Tooltip.Content><p>Vote count</p></Tooltip.Content>
          </Tooltip.Root>
        {/if}

        <!-- popularity -->
        {#if popularityFmt}
          <Tooltip.Root>
            <Tooltip.Trigger class="inline-flex max-w-fit cursor-help">
              <span class="inline-flex items-center gap-0.5 text-xs">
                <Flame class="size-3 text-orange-400" />
                {popularityFmt}
              </span>
            </Tooltip.Trigger>
            <Tooltip.Content><p>Popularity</p></Tooltip.Content>
          </Tooltip.Root>
        {/if}

        <!-- we only care about the status for TV shows -->
        {#if entry.tmdb_status && mediaType === "tv"}
          <span class="text-xs italic text-muted-foreground"
            ><strong>Status:</strong> {entry.tmdb_status}</span
          >
        {/if}
      </div>
    {/if}

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
  </div>
{/if}
