<script lang="ts">
  import ReclaimerrSVG from "$lib/components/svgs/reclaimerr-logo-svg.svelte";
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

<div class="dark">
  <div
    class="min-h-screen flex items-center justify-center bg-background px-4 text-foreground"
  >
    <div class="max-w-md w-full space-y-8">
      <div
        class="bg-card/90 backdrop-blur-xl rounded-lg shadow-2xl shadow-primary/10 p-8 border border-border"
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
              class="mb-4 rounded-lg border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive"
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
              <h1 class="text-4xl font-bold text-foreground mb-1">
                Reclaimerr
              </h1>
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
              class="input-hover-el"
              placeholder="Admin password"
              autocomplete="new-password"
              minlength={3}
              maxlength={64}
            />

            <!-- confirm password -->
            <Input
              id="confirm-password"
              type="password"
              bind:value={confirmPassword}
              disabled={loading}
              required
              class="input-hover-el"
              placeholder="Confirm password"
              autocomplete="new-password"
              minlength={3}
              maxlength={64}
            />

            <Button
              type="submit"
              disabled={loading}
              size="lg"
              class="w-full cursor-pointer hover:bg-primary-hover"
            >
              <ShieldCheck class="size-5 mr-2" />
              {loading ? "Creating account…" : "Create Admin Account"}
            </Button>
          </form>
        {/if}
      </div>
    </div>
  </div>
</div>
