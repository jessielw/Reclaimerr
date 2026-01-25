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

  const routes = {
    "/": Dashboard,
    "/movies": Movies,
    "/series": Series,
    "/users": Users,
    "/account": Account,
    "/settings": Settings,
  };

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
    <Sidebar />

    <main class="flex-1 overflow-y-auto">
      <Router {routes} />
    </main>
  </div>
{/if}

<ModeWatcher defaultMode="dark" />
<Toaster />
