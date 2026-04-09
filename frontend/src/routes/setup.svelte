<script lang="ts">
  import ReclaimerrSVG from "$lib/components/svgs/ReclaimerrLogoSVG.svelte";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { onMount } from "svelte";
  import ShieldCheck from "@lucide/svelte/icons/shield-check";

  let { onComplete }: { onComplete?: () => void } = $props();

  let password = $state("");
  let confirmPassword = $state("");
  let error = $state("");
  let loading = $state(false);
  let done = $state(false);

  // if setup is already complete, hand control back to the parent immediately.
  onMount(async () => {
    try {
      const res = await fetch("/api/setup/status");
      if (res.ok) {
        const data = await res.json();
        if (!data.needs_setup) {
          onComplete?.();
        }
      }
    } catch {
      // ignore in case server is still starting
    }
  });

  const handleSubmit = async () => {
    error = "";

    if (password !== confirmPassword) {
      error = "Passwords do not match.";
      return;
    }

    loading = true;
    try {
      const res = await fetch("/api/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password, confirm_password: confirmPassword }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        if (Array.isArray(data?.detail)) {
          error = data.detail.map((e: any) => e.msg).join(" ");
        } else {
          error = data?.detail ?? "Setup failed. Please try again.";
        }
        return;
      }

      done = true;
      setTimeout(() => onComplete?.(), 2000);
    } catch {
      error = "Could not reach the server. Please try again.";
    } finally {
      loading = false;
    }
  };
</script>

<div
  class="dark min-h-screen flex items-center justify-center bg-background px-4"
>
  <div class="max-w-md w-full space-y-8">
    <div
      class="bg-black/70 backdrop-blur-sm rounded-lg shadow-xl p-8 border border-primary"
    >
      {#if done}
        <div class="text-center space-y-4">
          <ShieldCheck class="w-16 h-16 text-primary mx-auto" />
          <h2 class="text-2xl font-bold text-foreground">Setup complete!</h2>
          <p class="text-muted-foreground">Redirecting to login…</p>
        </div>
      {:else}
        {#if error}
          <div
            class="mb-4 p-3 bg-red-900/50 border border-red-700 rounded text-red-200 text-sm"
          >
            {error}
          </div>
        {/if}

        <form
          onsubmit={(e) => {
            e.preventDefault();
            handleSubmit();
          }}
          class="space-y-4"
        >
          <!-- logo / title -->
          <div class="text-center">
            <div class="flex justify-center mb-4">
              <ReclaimerrSVG
                class="w-1/2 stroke-13 stroke-primary-stroke fill-primary"
              />
            </div>
            <h1 class="text-4xl font-bold text-foreground mb-1">Reclaimerr</h1>
            <p class="text-muted-foreground text-sm">
              Welcome! Create your admin account to get started.
            </p>
          </div>

          <div class="space-y-1">
            <p class="text-xs text-muted-foreground">
              Your username will be <span class="text-foreground font-medium"
                >admin</span
              >. You can add more users after setup.
            </p>
          </div>

          <!-- password -->
          <Input
            id="password"
            type="password"
            bind:value={password}
            disabled={loading}
            required
            class="w-full px-3 py-2 bg-gray-950/60! border border-gray-700 input-hover-el
              rounded-md text-white placeholder-gray-500"
            placeholder="Admin password"
            autocomplete="new-password"
            minlength={8}
            maxlength={64}
          />

          <!-- confirm password -->
          <Input
            id="confirm-password"
            type="password"
            bind:value={confirmPassword}
            disabled={loading}
            required
            class="w-full px-3 py-2 bg-gray-950/60! border border-gray-700 input-hover-el
              rounded-md text-white placeholder-gray-500"
            placeholder="Confirm password"
            autocomplete="new-password"
            minlength={8}
            maxlength={64}
          />

          <p class="text-xs text-muted-foreground">
            Min 8 characters · uppercase · lowercase · number · special
            character
          </p>

          <Button
            type="submit"
            disabled={loading}
            size="lg"
            class="flex justify-center w-full py-2 px-4 bg-primary hover:bg-primary-hover
              text-white font-medium rounded-md transition-colors cursor-pointer"
          >
            <ShieldCheck class="size-5 mr-2" />
            {loading ? "Creating account…" : "Create Admin Account"}
          </Button>
        </form>
      {/if}
    </div>
  </div>
</div>
