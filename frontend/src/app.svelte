<script lang="ts">
  import Router from "svelte-spa-router";
  import { auth } from "$lib/stores/auth";
  import { ModeWatcher } from "mode-watcher";
  import Sidebar from "$lib/components/sidebar.svelte";
  import Login from "./routes/login.svelte";
  import Setup from "./routes/setup.svelte";
  import Dashboard from "./routes/dashboard.svelte";
  import Requests from "./routes/requests/requests-entry.svelte";
  import Movies from "./routes/movies.svelte";
  import Series from "./routes/series.svelte";
  import Protected from "./routes/protected.svelte";
  import Candidates from "./routes/candidates.svelte";
  import Rules from "./routes/rules.svelte";
  import Settings from "./routes/settings.svelte";
  import { Toaster } from "$lib/components/ui/sonner/index.js";
  import SystemAlerts from "$lib/components/system-alerts.svelte";
  import { onMount } from "svelte";
  import Menu from "@lucide/svelte/icons/menu";
  import X from "@lucide/svelte/icons/x";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import logoImage from "$lib/assets/logo.png";

  const routes = {
    "/": Dashboard,
    "/movies": Movies,
    "/series": Series,
    "/protected": Protected,
    "/requests": Requests,
    "/candidates": Candidates,
    "/rules": Rules,
    "/settings": Settings,
    "/setup": Setup,
  };

  const appChannel = (import.meta.env.VITE_APP_CHANNEL ?? "dev")
    .toString()
    .toLowerCase();
  const isDevBuild = appChannel !== "release";

  let sidebarOpen = $state(false);
  let needsSetup = $state(false);

  function closeSidebar() {
    sidebarOpen = false;
  }

  onMount(async () => {
    // Check setup status before auth so we show the wizard immediately on
    // first run instead of briefly flashing the login screen.
    try {
      const res = await fetch("/api/setup/status");
      if (res.ok) {
        const data = await res.json();
        needsSetup = data.needs_setup ?? false;
      }
    } catch {
      // server not yet ready (auth.init() will handle the loading state)
    }
    auth.init();
  });
</script>

{#if $auth.loading}
  <!-- loading screen while checking authentication -->
  <div class="flex h-screen items-center justify-center bg-background">
    <div class="text-center">
      <div
        class="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-primary
        border-r-transparent"
      ></div>
      <p class="mt-4 text-muted-foreground">Loading...</p>
    </div>
  </div>
{:else if needsSetup}
  <!-- first run setup wizard -->
  <Setup onComplete={() => (needsSetup = false)} />
{:else if !$auth.isAuthenticated}
  <!-- show login screen if not authenticated -->
  <Login />
{:else}
  <!-- show main app if authenticated -->
  <Tooltip.Provider>
    <div class="flex h-screen bg-background">
      <!-- mobile header bar -->
      <div
        class="lg:hidden fixed top-0 left-0 right-0 z-30 bg-card border-b border-border px-4 py-3 flex
          items-center gap-3"
      >
        <button
          onclick={() => (sidebarOpen = !sidebarOpen)}
          class="p-2 hover:bg-accent rounded-lg transition-colors"
          aria-label="Toggle menu"
        >
          {#if sidebarOpen}
            <X class="w-6 h-6 text-foreground" />
          {:else}
            <Menu class="w-6 h-6 text-foreground" />
          {/if}
        </button>
        <img src={logoImage} alt="reclaimerr logo" class="w-6 h-6" />
        <h1 class="font-semibold text-lg text-foreground">Reclaimerr</h1>
        {#if isDevBuild}
          <span
            class="absolute right-1 top-1 z-10 rounded bg-destructive px-1.5 py-0.5 text-[10px]
              font-bold tracking-wide text-destructive-foreground pointer-events-none"
          >
            DEV
          </span>
        {/if}
      </div>

      <!-- mobile backdrop overlay -->
      {#if sidebarOpen}
        <button
          onclick={closeSidebar}
          class="lg:hidden fixed inset-0 bg-black/50 z-24 backdrop-blur-sm border-0 p-0 w-full h-full"
          aria-label="Close menu"
        ></button>
      {/if}

      <!-- sidebar: slide in on mobile, always visible on desktop -->
      <div
        class="{sidebarOpen
          ? 'translate-x-0'
          : '-translate-x-full'} lg:translate-x-0 fixed lg:static
          inset-y-0 left-0 transition-transform duration-300 ease-in-out pt-14 lg:pt-0 z-25"
      >
        <Sidebar onNavigate={closeSidebar} />
      </div>

      <!-- main content with top margin on mobile for fixed header -->
      <main class="flex-1 overflow-y-auto mt-16 lg:mt-0">
        <SystemAlerts />
        <Router {routes} />
        {#if isDevBuild}
          <span
            class="absolute right-5 top-1 z-10 rounded bg-destructive px-1.5 py-0.5 text-[10px]
              font-bold tracking-wide text-destructive-foreground pointer-events-none"
          >
            DEV
          </span>
        {/if}
      </main>
    </div>
  </Tooltip.Provider>
{/if}

<ModeWatcher defaultMode="dark" />

<Toaster richColors />
