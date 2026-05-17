<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { auth } from "$lib/stores/auth";
  import { get_api } from "$lib/api";

  const refreshIntervalMs = 5 * 60 * 1000;

  interface CandidatesPresenceResponse {
    has_candidates: boolean;
  }

  let hasCandidates = $state(false);
  let abortController: AbortController | null = null;
  let refreshInterval: ReturnType<typeof setInterval> | null = null;

  const loadPresence = async () => {
    if (!$auth.isAuthenticated) {
      hasCandidates = false;
      return;
    }

    if (abortController) abortController.abort();
    abortController = new AbortController();
    const signal = abortController.signal;

    try {
      const response = await get_api<CandidatesPresenceResponse>(
        "/api/media/candidates/presence",
        signal,
      );
      if (!signal.aborted) {
        hasCandidates = !!response.has_candidates;
      }
    } catch {
      if (!signal.aborted) hasCandidates = false;
    }
  };

  const handleVisibilityChange = () => {
    if (document.visibilityState === "visible") {
      loadPresence();
    }
  };

  onMount(() => {
    loadPresence();
    document.addEventListener("visibilitychange", handleVisibilityChange);
    refreshInterval = setInterval(loadPresence, refreshIntervalMs);
  });

  onDestroy(() => {
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    if (refreshInterval) clearInterval(refreshInterval);
    if (abortController) abortController.abort();
  });
</script>

{#if hasCandidates}
  <span
    class="ml-auto inline-flex size-2 rounded-full bg-amber-400 shrink-0"
    aria-label="Candidates available"
    title="Candidates available"
  ></span>
{/if}
