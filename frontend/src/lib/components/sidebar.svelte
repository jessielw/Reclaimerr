<script lang="ts">
  import { link, location } from "svelte-spa-router";
  import { auth } from "$lib/stores/auth";
  import ThemeToggle from "./theme-toggle.svelte";
  import logoImage from "$lib/assets/logo.png";
  import { VERSION } from "$lib/version";
  import House from "@lucide/svelte/icons/house";
  import ClapperBoard from "@lucide/svelte/icons/clapperboard";
  import Tv from "@lucide/svelte/icons/tv";
  import Settings from "@lucide/svelte/icons/settings";
  import DoorOpen from "@lucide/svelte/icons/door-open";
  import DoorClosed from "@lucide/svelte/icons/door-closed";
  import HardDrive from "@lucide/svelte/icons/hard-drive";
  import Ticket from "@lucide/svelte/icons/ticket";
  import Shield from "@lucide/svelte/icons/shield";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
  import { toTitleCase } from "$lib/utils/strings";
  import * as Avatar from "$lib/components/ui/avatar/index.js";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";

  // optional callback to close sidebar on mobile after navigation
  let { onNavigate = () => {} }: { onNavigate?: () => void } = $props();

  // nav items: path = route path, label = display text, icon = icon component
  // adminOnly = whether to show item only for admin users
  const navItems = [
    {
      path: "/",
      label: "Dashboard",
      icon: House,
      adminOnly: false,
      tooltip: null,
    },
    {
      path: "/movies",
      label: "Movies",
      icon: ClapperBoard,
      adminOnly: false,
      tooltip: null,
    },
    {
      path: "/series",
      label: "Series",
      icon: Tv,
      adminOnly: false,
      tooltip: null,
    },
    {
      path: "/protection-requests",
      label: "Requests",
      icon: Ticket,
      adminOnly: false,
      tooltip: "View and manage media deletion requests submitted by users",
    },
    {
      path: "/protected",
      label: "Protected",
      icon: Shield,
      adminOnly: false,
      tooltip:
        "View and manage protected media that won't be automatically deleted",
    },
    {
      path: "/candidates",
      label: "Candidates",
      icon: TriangleAlert,
      adminOnly: false,
      tooltip:
        "Review media that are candidates for deletion based on your retention settings",
    },
    {
      path: "/settings",
      label: "Settings",
      icon: Settings,
      adminOnly: false,
      tooltip: null,
    },
  ];

  // vars
  let logoutHovered = $state(false);

  // function to check if a nav item is active
  const isActive = (path: string): boolean => {
    return $location === path;
  };

  // logout handler
  const handleLogout = async () => {
    await auth.logout();
  };
</script>

<aside class="w-64 bg-card border-r border-border flex flex-col h-full">
  <!-- logo -->
  <div class="relative p-6 border-b border-border">
    <div class="flex just items-center gap-3">
      <div class="flex gap-3 items-center">
        <div class="w-12 h-12 flex items-center justify-center">
          <img src={logoImage} alt="reclaimerr logo" class="w-10 h-10" />
        </div>
        <h1 class="text-xl font-bold text-foreground">Reclaimerr</h1>
      </div>
    </div>
    <!-- theme toggle -->
    <ThemeToggle class="absolute top-1 right-1" />
  </div>

  <!-- navigation -->
  <nav class="flex-1 p-4 space-y-1 overflow-y-auto">
    {#each navItems as item}
      {#if !item.adminOnly || $auth.user?.role === "admin"}
        <Tooltip.Root>
          <!-- we'll only add a trigger if tooltip exists -->
          {#if item.tooltip}
            <Tooltip.Root>
              <Tooltip.Trigger>
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
              </Tooltip.Trigger>
              <Tooltip.Content>
                <p>{item.tooltip}</p>
              </Tooltip.Content>
            </Tooltip.Root>
          {:else}
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
          <Tooltip.Content>
            <p>{item.tooltip}</p>
          </Tooltip.Content>
        </Tooltip.Root>
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
              <Avatar.Root class="w-10 h-10 border-3 border-primary">
                <Avatar.Image src={$auth.user.avatar_url} alt="Avatar" />
                <Avatar.Fallback
                  >{$auth.user.username.charAt(0).toUpperCase()}
                </Avatar.Fallback>
              </Avatar.Root>
            {:else}
              <Avatar.Root
                class="w-10 h-10 text-2xl text-primary-foreground font-bold"
              >
                <Avatar.Fallback class="bg-primary"
                  >{$auth.user.username
                    .charAt(0)
                    .toUpperCase()}</Avatar.Fallback
                >
              </Avatar.Root>
            {/if}
            <div>
              <div class="text-sm font-medium text-foreground">
                <p class="truncate overflow-hidden whitespace-nowrap w-40">
                  {$auth.user.display_name || $auth.user.username}
                </p>
              </div>
              <div class="text-xs text-muted-foreground">
                {toTitleCase($auth.user.role)}
              </div>
            </div>
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
