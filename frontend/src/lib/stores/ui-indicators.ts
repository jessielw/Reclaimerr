import { get, writable } from "svelte/store";

import { get_api } from "$lib/api";
import { auth } from "$lib/stores/auth";
import type { UiIndicatorsResponse } from "$lib/types/shared";

const POLL_INTERVAL_MS = 60 * 1000;
const INVALIDATE_DEBOUNCE_MS = 150;
export const UI_INDICATORS_INVALIDATE_EVENT =
  "reclaimerr:ui-indicators:invalidate";

type UiIndicatorsState = {
  hasCandidates: boolean;
  hasPendingRequests: boolean;
  hasPendingProtectionRequests: boolean;
  hasPendingDeleteRequests: boolean;
  updateAvailable: boolean;
  latestVersion: string | null;
  latestReleaseUrl: string | null;
  lastCheckedAt: string | null;
};

const initialState = (): UiIndicatorsState => ({
  hasCandidates: false,
  hasPendingRequests: false,
  hasPendingProtectionRequests: false,
  hasPendingDeleteRequests: false,
  updateAvailable: false,
  latestVersion: null,
  latestReleaseUrl: null,
  lastCheckedAt: null,
});

function createUiIndicatorsStore() {
  const { subscribe, set } = writable<UiIndicatorsState>(initialState());

  let started = false;
  let refreshInterval: ReturnType<typeof setInterval> | null = null;
  let invalidateTimer: ReturnType<typeof setTimeout> | null = null;
  let inFlight: Promise<void> | null = null;
  let authUnsubscribe: (() => void) | null = null;

  const clearInvalidateTimer = () => {
    if (!invalidateTimer) return;
    clearTimeout(invalidateTimer);
    invalidateTimer = null;
  };

  const reset = () => {
    set(initialState());
  };

  const refreshNow = async () => {
    const authState = get(auth);
    if (!authState.isAuthenticated) {
      reset();
      return;
    }

    if (inFlight) return inFlight;

    inFlight = (async () => {
      try {
        const response = await get_api<UiIndicatorsResponse>(
          "/api/info/ui-indicators",
        );
        set({
          hasCandidates: !!response.has_candidates,
          hasPendingRequests: !!response.has_pending_requests,
          hasPendingProtectionRequests:
            !!response.has_pending_protection_requests,
          hasPendingDeleteRequests: !!response.has_pending_delete_requests,
          updateAvailable: !!response.update_available,
          latestVersion: response.latest_version,
          latestReleaseUrl: response.latest_release_url,
          lastCheckedAt: response.last_checked_at,
        });
      } catch {
        // keep previous state on transient failures
      } finally {
        inFlight = null;
      }
    })();

    return inFlight;
  };

  const invalidate = () => {
    if (!started) return;
    if (invalidateTimer) return;
    invalidateTimer = setTimeout(() => {
      invalidateTimer = null;
      void refreshNow();
    }, INVALIDATE_DEBOUNCE_MS);
  };

  const handleVisibilityChange = () => {
    if (document.visibilityState === "visible") {
      void refreshNow();
    }
  };

  const handleInvalidateEvent = () => {
    invalidate();
  };

  const start = () => {
    if (started) return;
    started = true;

    authUnsubscribe = auth.subscribe((state) => {
      if (!state.isAuthenticated) {
        reset();
        return;
      }
      void refreshNow();
    });

    void refreshNow();
    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener(
      UI_INDICATORS_INVALIDATE_EVENT,
      handleInvalidateEvent,
    );
    refreshInterval = setInterval(() => {
      void refreshNow();
    }, POLL_INTERVAL_MS);
  };

  const stop = () => {
    if (!started) return;
    started = false;
    clearInvalidateTimer();
    if (refreshInterval) {
      clearInterval(refreshInterval);
      refreshInterval = null;
    }
    if (authUnsubscribe) {
      authUnsubscribe();
      authUnsubscribe = null;
    }
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    window.removeEventListener(
      UI_INDICATORS_INVALIDATE_EVENT,
      handleInvalidateEvent,
    );
  };

  return {
    subscribe,
    start,
    stop,
    refreshNow,
    invalidate,
  };
}

export const uiIndicators = createUiIndicatorsStore();
