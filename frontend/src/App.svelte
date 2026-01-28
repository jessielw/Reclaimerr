<script lang="ts">
  import Router from "svelte-spa-router";
  import { auth } from "./lib/stores/auth";
  import { ModeWatcher } from "mode-watcher";
  import Sidebar from "./lib/components/Sidebar.svelte";
  import Login from "./routes/Login.svelte";
  import Dashboard from "./routes/Dashboard.svelte";
  import Movies from "./routes/Movies.svelte";
  import Series from "./routes/Series.svelte";
  import Users from "./routes/Users.svelte";
  import Account from "./routes/Account.svelte";
  import Settings from "./routes/Settings.svelte";
  import { Toaster } from "$lib/components/ui/sonner/index.js";
  import { onMount } from "svelte";
  import Menu from "@lucide/svelte/icons/menu";
  import X from "@lucide/svelte/icons/x";

  const routes = {
    "/": Dashboard,
    "/movies": Movies,
    "/series": Series,
    "/users": Users,
    "/account": Account,
    "/settings": Settings,
  };

  let sidebarOpen = $state(false);

  function closeSidebar() {
    sidebarOpen = false;
  }

  onMount(() => {
    auth.init();
  });
</script>

{#if $auth.loading}
  <!-- loading screen while checking authentication -->
  <div class="flex h-screen items-center justify-center bg-background">
    <div class="text-center">
      <div
        class="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-primary border-r-transparent"
      ></div>
      <p class="mt-4 text-muted-foreground">Loading...</p>
    </div>
  </div>
{:else if !$auth.isAuthenticated}
  <!-- show login screen if not authenticated -->
  <Login />
{:else}
  <!-- show main app if authenticated -->
  <div class="flex h-screen bg-background">
    <!-- mobile header bar -->
    <div
      class="lg:hidden fixed top-0 left-0 right-0 z-50 bg-card border-b border-border px-4 py-3 flex items-center gap-3"
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
      <h1 class="font-semibold text-lg text-foreground">Vacuumerr</h1>
    </div>

    <!-- mobile backdrop overlay -->
    {#if sidebarOpen}
      <button
        onclick={closeSidebar}
        class="lg:hidden fixed inset-0 bg-black/50 z-40 backdrop-blur-sm border-0 p-0 w-full h-full"
        aria-label="Close menu"
      ></button>
    {/if}

    <!-- sidebar: slide in on mobile, always visible on desktop -->
    <div
      class="{sidebarOpen
        ? 'translate-x-0'
        : '-translate-x-full'} lg:translate-x-0 fixed lg:static
        inset-y-0 left-0 z-40 transition-transform duration-300 ease-in-out pt-14 lg:pt-0"
    >
      <Sidebar onNavigate={closeSidebar} />
    </div>

    <!-- main content with top padding on mobile for header -->
    <main class="flex-1 overflow-y-auto pt-14 lg:pt-0">
      <Router {routes} />
    </main>
  </div>
{/if}

<ModeWatcher defaultMode="dark" />
<Toaster />
