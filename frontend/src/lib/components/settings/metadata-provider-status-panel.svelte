<script lang="ts">
  import { Badge } from "$lib/components/ui/badge/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { formatDistanceToNow } from "$lib/utils/date";
  import type { MetadataProviderStatus } from "$lib/types/shared";

  interface Props {
    provider: MetadataProviderStatus | null;
    loading?: boolean;
    error?: string | null;
  }

  let { provider, loading = false, error = null }: Props = $props();

  const formatCount = (value: number | null | undefined) =>
    value == null ? "Unknown" : value.toLocaleString();

  const formatPercent = (value: number) => `${value.toFixed(1)}%`;

  const formatRelative = (value: string | null | undefined) =>
    value ? formatDistanceToNow(value) : "Never";
</script>

<div class="rounded-lg border border-border bg-muted/20 p-4">
  <div class="flex items-start justify-between gap-3">
    <div>
      <h3 class="text-sm font-semibold text-foreground">Usage and Coverage</h3>
      <p class="mt-1 text-xs text-muted-foreground">
        Request usage is tracked per refresh run; coverage counts active library
        rows.
      </p>
    </div>
    {#if provider}
      <Badge class={provider.enabled ? "bg-add/70" : "bg-gray-500"}>
        {provider.enabled ? "Enabled" : "Disabled"}
      </Badge>
    {/if}
  </div>

  {#if loading}
    <div class="flex items-center gap-2 py-4 text-sm text-muted-foreground">
      <Spinner class="size-4" />
      Loading provider status...
    </div>
  {:else if error}
    <p class="mt-4 text-sm text-destructive">{error}</p>
  {:else if !provider}
    <p class="mt-4 text-sm text-muted-foreground">
      Provider status is not available yet.
    </p>
  {:else}
    <div class="mt-4 grid gap-3 md:grid-cols-2">
      <div class="rounded-md border border-border bg-card p-3">
        <p
          class="text-xs font-medium uppercase tracking-wide text-muted-foreground"
        >
          Last Refresh Usage
        </p>
        <p class="mt-1 text-lg font-semibold text-foreground">
          {formatCount(provider.last_run_requests)} /
          {formatCount(
            provider.last_run_request_limit ?? provider.request_limit,
          )}
        </p>
        <p class="mt-1 text-xs text-muted-foreground">
          Requests used by this provider in the last refresh run.
        </p>
        <p class="mt-1 text-xs text-muted-foreground">
          Current delay: {provider.request_delay_seconds.toFixed(1)}s between
          requests.
        </p>
      </div>

      <div class="rounded-md border border-border bg-card p-3">
        <p
          class="text-xs font-medium uppercase tracking-wide text-muted-foreground"
        >
          Total Coverage
        </p>
        <p class="mt-1 text-lg font-semibold text-foreground">
          {provider.coverage.total.covered.toLocaleString()} /
          {provider.coverage.total.total.toLocaleString()}
          <span class="text-sm font-normal text-muted-foreground">
            ({formatPercent(provider.coverage.total.percent)})
          </span>
        </p>
        <p class="mt-1 text-xs text-muted-foreground">
          Active movies and series with cached ratings from {provider.name}.
        </p>
      </div>
    </div>

    <div class="mt-3 grid gap-3 md:grid-cols-2">
      <div class="rounded-md border border-border bg-card p-3">
        <p
          class="text-xs font-medium uppercase tracking-wide text-muted-foreground"
        >
          Movie Coverage
        </p>
        <p class="mt-1 text-sm text-foreground">
          {provider.coverage.movies.covered.toLocaleString()} /
          {provider.coverage.movies.total.toLocaleString()}
          ({formatPercent(provider.coverage.movies.percent)})
        </p>
      </div>
      <div class="rounded-md border border-border bg-card p-3">
        <p
          class="text-xs font-medium uppercase tracking-wide text-muted-foreground"
        >
          Series Coverage
        </p>
        <p class="mt-1 text-sm text-foreground">
          {provider.coverage.series.covered.toLocaleString()} /
          {provider.coverage.series.total.toLocaleString()}
          ({formatPercent(provider.coverage.series.percent)})
        </p>
      </div>
    </div>

    <div class="mt-3 space-y-1 text-xs text-muted-foreground">
      <p>Last checked: {formatRelative(provider.last_checked_at)}</p>
      <p>
        Last successful refresh: {formatRelative(
          provider.last_successful_refresh_at,
        )}
      </p>
      {#if provider.disabled_reason}
        <p class="text-yellow-500">
          Provider stopped last run: {provider.disabled_reason}
        </p>
      {/if}
      {#if provider.last_error}
        <p class="text-destructive">
          Last refresh error: {provider.last_error}
        </p>
      {/if}
      {#if !provider.configured}
        <p class="text-yellow-500">
          Save an API key to enable status collection for this provider.
        </p>
      {/if}
    </div>
  {/if}
</div>
