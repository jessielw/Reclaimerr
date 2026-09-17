<script lang="ts">
  import { onMount } from "svelte";
  import { auth } from "$lib/stores/auth";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";

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

  // Only same-tab sign-ins land here now. A popup flow ends on a dead-end page
  // served by the backend that closes itself, because sending the popup back into
  // the app is what left users looking at a second copy of Reclaimerr (#353).
  onMount(async () => {
    const error = readAuthError();
    if (error) {
      message = "Sign in failed. Returning to the login page...";
      window.setTimeout(() => {
        window.location.replace(`/?auth_error=${encodeURIComponent(error)}`);
      }, 250);
      return;
    }

    await auth.init();
    message = "Sign in complete.";
    window.location.replace("/#/");
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
