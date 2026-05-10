<script lang="ts">
  import { onMount } from "svelte";
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
  import Filter from "@lucide/svelte/icons/filter";
  import SlidersHorizontal from "@lucide/svelte/icons/sliders-horizontal";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
  import Check from "@lucide/svelte/icons/check";
  import { toTitleCase } from "$lib/utils/strings";
  import * as Avatar from "$lib/components/ui/avatar/index.js";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import * as DropdownMenu from "$lib/components/ui/dropdown-menu/index.js";
  import { createFilterState } from "$lib/utils/pagination";

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
      path: "/requests",
      label: "Requests",
      icon: Ticket,
      adminOnly: false,
      tooltip: "View and manage delete and protection requests",
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
      path: "/rules",
      label: "Rules",
      icon: Filter,
      adminOnly: true,
      tooltip: "Create and manage cleanup rules",
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
  let menuOpen = $state(false);
  let hiddenPaths = $state<string[]>([]);

  const hiddenNavStore = createFilterState<string[]>(
    "sidebar_hidden_paths",
    [],
  );
  const lockedNavPaths = new Set(["/settings"]);

  const visibleNavItems = $derived(
    navItems.filter(
      (item) =>
        lockedNavPaths.has(item.path) || !hiddenPaths.includes(item.path),
    ),
  );

  const customizableNavItems = $derived(
    navItems.filter((item) => !lockedNavPaths.has(item.path)),
  );

  // function to check if a nav item is active
  const isActive = (path: string): boolean => {
    return $location === path;
  };

  // logout handler
  const handleLogout = async () => {
    await auth.logout();
  };

  const isShown = (path: string): boolean => !hiddenPaths.includes(path);

  const toggleNavVisibility = (path: string) => {
    if (lockedNavPaths.has(path)) return;
    if (hiddenPaths.includes(path)) {
      hiddenPaths = hiddenPaths.filter((p) => p !== path);
      return;
    }
    hiddenPaths = [...hiddenPaths, path];
  };

  const resetNavVisibility = () => {
    hiddenPaths = [];
  };

  $effect(() => {
    hiddenNavStore.save(hiddenPaths);
  });

  onMount(() => {
    const stored = hiddenNavStore.getInitial();
    hiddenPaths = Array.isArray(stored) ? stored : [];
  });
</script>

<aside class="w-64 bg-card border-r border-border flex flex-col h-full">
  <!-- logo -->
  <div class="relative p-4 border-b border-border">
    <a
      href="/"
      use:link
      onclick={onNavigate}
      class="inline-flex items-center gap-3 rounded-lg p-1 -m-1 hover:text-primary"
    >
      <div class="w-12 h-12 flex items-center justify-center">
        <img src={logoImage} alt="reclaimerr logo" class="w-10 h-10" />
      </div>
      <h1 class="text-xl font-bold text-foreground hover:text-inherit">
        Reclaimerr
      </h1>
    </a>
    <!-- theme toggle -->
    <ThemeToggle class="absolute top-1 right-1" />
  </div>

  <!-- navigation -->
  <nav class="flex-1 relative p-4 overflow-y-auto">
    <!-- navigation customization -->
    <DropdownMenu.Root bind:open={menuOpen}>
      <DropdownMenu.Trigger>
        {#snippet child({ props })}
          <button
            {...props}
            type="button"
            class="absolute left-1 bottom-1 z-10 cursor-pointer text-muted-foreground hover:text-primary
              rounded p-1 hover:bg-accent"
            aria-label="Customize navigation"
          >
            <SlidersHorizontal class="size-4" />
          </button>
        {/snippet}
      </DropdownMenu.Trigger>
      <DropdownMenu.Content align="start" class="w-52">
        {#each customizableNavItems as item}
          {#if !item.adminOnly || $auth.user?.role === "admin"}
            <DropdownMenu.Item
              class="cursor-pointer data-highlighted:text-primary"
              onSelect={(event) => {
                event.preventDefault();
                toggleNavVisibility(item.path);
              }}
            >
              <span
                class="inline-flex w-full items-center justify-between gap-2"
              >
                <div class="inline-flex items-center gap-2">
                  <item.icon class="text-inherit" />
                  {item.label}
                </div>
                {#if isShown(item.path)}
                  <Check class="size-4 text-inherit" />
                {:else}
                  <span class="size-4 inline-block"></span>
                {/if}
              </span>
            </DropdownMenu.Item>
          {/if}
        {/each}
        <DropdownMenu.Separator />
        <DropdownMenu.Item
          class="cursor-pointer data-highlighted:text-destructive"
          onSelect={(event) => {
            event.preventDefault();
            resetNavVisibility();
          }}
        >
          <span class="inline-flex items-center gap-2">
            <RotateCcw class="size-4 text-inherit" />
            Reset
          </span>
        </DropdownMenu.Item>
      </DropdownMenu.Content>
    </DropdownMenu.Root>

    <!-- navigation items -->
    {#each visibleNavItems as item}
      {#if !item.adminOnly || $auth.user?.role === "admin"}
        <Tooltip.Root>
          <!-- we'll only add a trigger if tooltip exists -->
          {#if item.tooltip}
            <Tooltip.Root>
              <Tooltip.Trigger class="w-full">
                <a
                  href={item.path}
                  use:link
                  onclick={onNavigate}
                  class="flex items-center gap-3 px-6 py-3 rounded-lg transition-colors duration-200 ml-3
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
              class="flex items-center gap-3 px-6 py-3 rounded-lg transition-colors duration-200 ml-3
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

  <div class="flex flex-col p-2 border-t border-border space-y-3">
    {#if $auth.user}
      <div>
        <div class="flex items-center">
          <div
            class="flex flex-row flex-1 px-3 pr-0 py-2 bg-secondary rounded-lg gap-3"
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
            hover:bg-destructive/10 hover:text-destructive transition-colors duration-200 cursor-pointer mt-1"
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
  <div class="flex p-3 border-t border-border space-3 gap-3 items-center">
    <HardDrive class="text-muted-foreground" />
    <div class="text-xs text-muted-foreground text-center">
      Reclaimerr v{VERSION}
    </div>
  </div>
</aside>
