<script lang="ts">
  import { link, location } from "svelte-spa-router";
  import { auth } from "../stores/auth";
  import ThemeToggle from "./ThemeToggle.svelte";
  import logoImage from "../assets/logo.png";
  import { VERSION } from "$lib/version";
  import House from "@lucide/svelte/icons/house";
  import ClapperBoard from "@lucide/svelte/icons/clapperboard";
  import Tv from "@lucide/svelte/icons/tv";
  import Settings from "@lucide/svelte/icons/settings";
  import DoorOpen from "@lucide/svelte/icons/door-open";
  import DoorClosed from "@lucide/svelte/icons/door-closed";
  import HardDrive from "@lucide/svelte/icons/hard-drive";
  import Ticket from "@lucide/svelte/icons/ticket";
  import { toTitleCase } from "$lib/utils/strings";

  // optional callback to close sidebar on mobile after navigation
  let { onNavigate = () => {} }: { onNavigate?: () => void } = $props();

  // nav items: path = route path, label = display text, icon = icon component
  // adminOnly = whether to show item only for admin users
  const navItems = [
    { path: "/", label: "Dashboard", icon: House, adminOnly: false },
    { path: "/movies", label: "Movies", icon: ClapperBoard, adminOnly: false },
    { path: "/series", label: "Series", icon: Tv, adminOnly: false },
    { path: "/requests", label: "Requests", icon: Ticket, adminOnly: false },
    { path: "/settings", label: "Settings", icon: Settings, adminOnly: false },
  ];

  // vars
  let logoutHovered = $state(false);

  // function to check if a nav item is active
  const isActive = (path: string): boolean => {
    return $location === path;
  };

  // logout handler
  const handleLogout = () => {
    auth.logout();
  };
</script>

<aside class="w-64 bg-card border-r border-border flex flex-col h-full">
  <!-- logo -->
  <div class="p-6 border-b border-border">
    <div class="flex items-center gap-3">
      <div class="flex gap-3">
        <div class="w-12 h-12 flex items-center justify-center">
          <img src={logoImage} alt="reclaimerr logo" class="w-10 h-10" />
        </div>
        <div>
          <h1 class="text-xl font-bold text-foreground">Reclaimerr</h1>
          <p class="text-xs text-muted-foreground">Media Cleanup</p>
        </div>
      </div>
      <!-- theme toggle -->
      <ThemeToggle />
    </div>
  </div>

  <!-- navigation -->
  <nav class="flex-1 p-4 space-y-1 overflow-y-auto">
    {#each navItems as item}
      {#if !item.adminOnly || $auth.user?.role === "admin"}
        <a
          href={item.path}
          use:link
          onclick={onNavigate}
          class="flex items-center gap-3 px-4 py-3 rounded-lg transition-colors duration-200
               {isActive(item.path)
            ? 'bg-primary text-primary-foreground'
            : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'}"
        >
          <item.icon />
          <span class="font-medium">{item.label}</span>
        </a>
      {/if}
    {/each}
  </nav>

  <div class="flex flex-col p-4 border-t border-border space-y-3">
    {#if $auth.user}
      <div>
        <div class="flex items-center gap-2">
          <div
            class="flex flex-row flex-1 px-3 py-2 bg-secondary rounded-lg gap-3"
          >
            <!-- user avatar -->
            {#if $auth.user.avatar_url}
              <div>
                <img
                  src={$auth.user.avatar_url}
                  alt="Avatar"
                  class="w-10 h-10 rounded-full object-cover border-3 border-primary"
                />
              </div>
            {:else}
              <div
                class="w-10 h-10 rounded-full bg-primary text-primary-foreground flex items-center
                  justify-center text-2xl font-bold border-3 border-primary"
              >
                {$auth.user.username.charAt(0).toUpperCase()}
              </div>
            {/if}
            <span>
              <div class="text-sm font-medium text-foreground">
                {$auth.user.display_name || $auth.user.username}
              </div>
              <div class="text-xs text-muted-foreground">
                {toTitleCase($auth.user.role)}
              </div>
            </span>
          </div>
        </div>

        <!-- logout button -->
        <button
          onmouseenter={() => (logoutHovered = true)}
          onmouseleave={() => (logoutHovered = false)}
          onclick={handleLogout}
          class="w-full flex justify-center items-center gap-3 px-4 py-2 rounded-lg text-muted-foreground
            hover:bg-destructive/10 hover:text-destructive transition-colors duration-200 cursor-pointer mt-3"
        >
          {#if logoutHovered}
            <DoorOpen />
          {:else}
            <DoorClosed />
          {/if}
          <span class="font-medium">Logout</span>
        </button>
      </div>
    {/if}
  </div>

  <!-- footer -->
  <div class="flex p-4 border-t border-border space-3 gap-3 items-center">
    <HardDrive class="text-muted-foreground" />
    <div class="text-xs text-muted-foreground text-center">
      Reclaimerr v{VERSION}
    </div>
  </div>
</aside>
