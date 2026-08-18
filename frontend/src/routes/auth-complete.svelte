<script lang="ts">
  import { onMount } from "svelte";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";

  type AuthCompleteMessage = {
    type: "reclaimerr-auth-complete";
    error: string | null;
  };

  let message = $state("Completing sign in...");

  const readAuthError = (): string | null => {
    if (typeof window === "undefined") return null;

    const searchParams = new URLSearchParams(window.location.search);
    const queryError = searchParams.get("auth_error");
    if (queryError) return queryError;

    const hash = window.location.hash;
    const queryStart = hash.indexOf("?");
    if (queryStart === -1) return null;

    return new URLSearchParams(hash.slice(queryStart + 1)).get("auth_error");
  };

  onMount(() => {
    const error = readAuthError();
    const payload: AuthCompleteMessage = {
      type: "reclaimerr-auth-complete",
      error,
    };

    // window.open(url, "reclaimerr-auth", …) names the popup's browsing context.
    // Unlike window.opener, that name survives cross-origin navigation (through
    // Plex's auth pages), so it reliably tells us whether we're really running
    // inside that popup vs. having driven the whole flow in the original tab
    // (e.g. because the popup was blocked and login.svelte fell back to a
    // same-tab redirect).
    const isPopupWindow = window.name === "reclaimerr-auth";

    try {
      const channel = new BroadcastChannel("reclaimerr-auth");
      channel.postMessage(payload);
      channel.close();
    } catch {
      // BroadcastChannel is a convenience; postMessage covers popup openers
    }

    if (window.opener && !window.opener.closed) {
      window.opener.postMessage(payload, window.location.origin);
    }

    message = error
      ? "Sign in failed. You can close this window."
      : "Sign in complete.";

    if (!error) {
      if (isPopupWindow) {
        // The opener has already been notified above. Try to close this popup,
        // but some browsers (especially after navigating through a third-party
        // auth page) silently refuse script-initiated close. In that case, don't
        // hijack this leftover window into loading the app inline — just let the
        // user close it manually.
        window.setTimeout(() => window.close(), 250);
        window.setTimeout(() => {
          if (!window.closed) {
            message = "Sign in complete. You can close this tab.";
          }
        }, 1000);
      } else {
        // No popup was involved (it was blocked, so this tab drove the whole
        // flow itself) — there's nothing else to return to, so go home.
        window.setTimeout(() => {
          window.location.href = "/";
        }, 250);
      }
    }
  });
</script>

<div
  class="dark flex h-screen items-center justify-center bg-background text-foreground"
>
  <div class="flex flex-col items-center gap-4 text-center">
    <Spinner class="size-8 text-primary" />
    <p class="text-sm text-muted-foreground">{message}</p>
  </div>
</div>
