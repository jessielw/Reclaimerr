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
      window.setTimeout(() => window.close(), 250);
      window.setTimeout(() => {
        window.location.href = "/";
      }, 1000);
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
