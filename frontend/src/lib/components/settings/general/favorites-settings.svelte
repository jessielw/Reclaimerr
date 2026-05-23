<script lang="ts">
  import { onDestroy } from "svelte";
  import { get_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import CompactPagination from "$lib/components/compact-pagination.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import Eye from "@lucide/svelte/icons/eye";
  import EyeOff from "@lucide/svelte/icons/eye-off";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
  import Copy from "@lucide/svelte/icons/copy";
  import UserStar from "@lucide/svelte/icons/user-star";
  import { toast } from "svelte-sonner";
  import {
    MediaType,
    type FavoritesMediaEntry,
    type FavoritesUserLookup,
    type PaginatedFavoritesMediaResponse,
  } from "$lib/types/shared";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";

  const FAVORITES_MEDIA_PER_PAGE = 25;
  const FAVORITES_MEDIA_VISIBLE_USERS = 5;

  interface Props {
    favoritesIgnoreEnabled?: boolean;
    favoritesProtectAllUsers?: boolean;
    favoritesUsernamesInput?: string;
  }

  let {
    favoritesIgnoreEnabled = $bindable(false),
    favoritesProtectAllUsers = $bindable(false),
    favoritesUsernamesInput = $bindable(""),
  }: Props = $props();

  let favoritesUsersOpen = $state(false);
  let favoritesUsersLoading = $state(false);
  let favoritesUsersError = $state<string | null>(null);
  let favoritesUsers = $state<FavoritesUserLookup[]>([]);
  let usersQuery = $state("");
  let favoritesMediaOpen = $state(false);
  let favoritesMediaLoading = $state(false);
  let favoritesMediaError = $state<string | null>(null);
  let favoritesMediaData = $state<PaginatedFavoritesMediaResponse | null>(null);
  let favoritesMediaSearch = $state("");
  let favoritesMediaUsername = $state("");
  let favoritesMediaType = $state<"all" | MediaType>("all");
  let favoritesMediaPage = $state(1);
  let favoritesMediaSearchTimer: ReturnType<typeof setTimeout> | null = null;

  const filteredUsers = $derived.by(() => {
    const needle = usersQuery.trim().toLowerCase();
    if (!needle) return favoritesUsers;
    return favoritesUsers.filter((user) =>
      user.username.toLowerCase().includes(needle),
    );
  });

  const loadFavoritesUsers = async (forceRefresh = false) => {
    favoritesUsersLoading = true;
    favoritesUsersError = null;
    try {
      const params = new URLSearchParams();
      if (forceRefresh) params.set("refresh", "true");
      const query = params.toString();
      favoritesUsers = await get_api<FavoritesUserLookup[]>(
        `/api/settings/general/favorites-users${query ? `?${query}` : ""}`,
      );
    } catch (error) {
      favoritesUsersError =
        error instanceof Error ? error.message : "Failed to load users";
      favoritesUsers = [];
    } finally {
      favoritesUsersLoading = false;
    }
  };

  const toggleFavoritesUsersPanel = async () => {
    favoritesUsersOpen = !favoritesUsersOpen;
    if (favoritesUsersOpen && favoritesUsers.length === 0) {
      await loadFavoritesUsers();
    }
  };

  const copyFavoritesUsers = async () => {
    const text = favoritesUsers.map((item) => item.username).join("\n");
    if (!text.trim()) {
      toast.info("No users available to copy.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Copied favorites usernames.");
    } catch {
      toast.error("Failed to copy usernames to clipboard.");
    }
  };

  const copyFavoritesOnlyUsers = async () => {
    const text = favoritesUsers
      .filter((item) => item.has_favorites)
      .map((item) => item.username)
      .join("\n");
    if (!text.trim()) {
      toast.info("No users with favorites available to copy.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Copied users with favorites.");
    } catch {
      toast.error("Failed to copy usernames to clipboard.");
    }
  };

  const loadFavoritesMedia = async (
    page: number = favoritesMediaPage,
    forceRefresh = false,
  ) => {
    favoritesMediaLoading = true;
    favoritesMediaError = null;
    favoritesMediaPage = page;
    try {
      const params = new URLSearchParams({
        page: page.toString(),
        per_page: FAVORITES_MEDIA_PER_PAGE.toString(),
      });
      const search = favoritesMediaSearch.trim();
      if (search) params.set("search", search);
      const username = favoritesMediaUsername.trim();
      if (username) params.set("username", username);
      if (favoritesMediaType !== "all") {
        params.set("media_type", favoritesMediaType);
      }
      if (forceRefresh) params.set("refresh", "true");
      favoritesMediaData = await get_api<PaginatedFavoritesMediaResponse>(
        `/api/settings/general/favorites-media?${params.toString()}`,
      );
    } catch (error) {
      favoritesMediaError =
        error instanceof Error
          ? error.message
          : "Failed to load favorites media";
      favoritesMediaData = null;
    } finally {
      favoritesMediaLoading = false;
    }
  };

  const toggleFavoritesMediaPanel = async () => {
    favoritesMediaOpen = !favoritesMediaOpen;
    if (favoritesMediaOpen && !favoritesMediaData) {
      await loadFavoritesMedia(1);
    }
  };

  const scheduleFavoritesMediaFilterReload = () => {
    if (favoritesMediaSearchTimer) clearTimeout(favoritesMediaSearchTimer);
    favoritesMediaSearchTimer = setTimeout(() => {
      if (!favoritesMediaOpen) return;
      loadFavoritesMedia(1);
    }, 300);
  };

  const visibleFavoriteUsers = (entry: FavoritesMediaEntry) =>
    entry.favorite_users.slice(0, FAVORITES_MEDIA_VISIBLE_USERS);

  const hiddenFavoriteUsers = (entry: FavoritesMediaEntry) =>
    entry.favorite_users.slice(FAVORITES_MEDIA_VISIBLE_USERS);

  const copySingleUser = async (username: string) => {
    try {
      await navigator.clipboard.writeText(username);
      toast.success(`Copied ${username}.`);
    } catch {
      toast.error("Failed to copy username.");
    }
  };

  onDestroy(() => {
    if (favoritesMediaSearchTimer) clearTimeout(favoritesMediaSearchTimer);
  });
</script>

<div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
  <div class="flex items-center justify-between mb-1">
    <h3 class="font-semibold text-foreground">Ignore User Favorites</h3>
    <Switch id="favoritesIgnoreEnabled" bind:checked={favoritesIgnoreEnabled} />
  </div>
  <p class="text-muted-foreground text-sm">
    Skip cleanup candidates when the movie or series appears in selected user
    favorites on Emby/Jellyfin.
  </p>

  {#if favoritesIgnoreEnabled}
    <div class="mt-4 space-y-3">
      <Label class="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          class="cursor-pointer"
          bind:checked={favoritesProtectAllUsers}
        />
        Protect favorites for all users
      </Label>

      {#if !favoritesProtectAllUsers}
        <div class="space-y-2">
          <div class="flex items-center justify-between gap-2">
            <Label for="favoritesUsernames" class="mb-0">
              <span class="text-sm text-foreground">Usernames</span>
            </Label>
            <Button
              size="sm"
              class="cursor-pointer gap-1"
              onclick={toggleFavoritesUsersPanel}
            >
              {#if favoritesUsersOpen}
                <EyeOff class="size-4" />
              {:else}
                <Eye class="size-4" />
              {/if}
              Users
            </Button>
          </div>
          <textarea
            id="favoritesUsernames"
            class="input-hover-el min-h-20 w-full rounded-md border border-input bg-background
              px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground"
            placeholder="user1, user2"
            bind:value={favoritesUsernamesInput}
            disabled={favoritesProtectAllUsers}
          ></textarea>
          <p class="text-xs text-muted-foreground mt-1">
            Comma or newline separated. Matching is case-insensitive.
          </p>
        </div>

        {#if favoritesUsersOpen}
          <div
            class="rounded-md border border-border bg-background/40 p-3 space-y-3"
          >
            <div class="flex flex-wrap items-center justify-between gap-2">
              <p class="text-xs text-muted-foreground">
                {favoritesUsers.length} user(s) loaded from media servers.
              </p>
              <div class="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  class="cursor-pointer"
                  onclick={() => loadFavoritesUsers(true)}
                  disabled={favoritesUsersLoading}
                >
                  <RotateCcw class="size-4" />
                  Refresh
                </Button>
                <Button
                  size="sm"
                  class="cursor-pointer"
                  onclick={copyFavoritesUsers}
                  disabled={favoritesUsers.length === 0}
                >
                  <Copy class="size-4" />
                  Copy all
                </Button>
                <Button
                  size="sm"
                  class="cursor-pointer"
                  onclick={copyFavoritesOnlyUsers}
                  disabled={!favoritesUsers.some((item) => item.has_favorites)}
                >
                  <UserStar class="size-4" />
                  Copy favorites only
                </Button>
              </div>
            </div>

            {#if favoritesUsersLoading}
              <p class="text-sm text-muted-foreground">Loading users...</p>
            {:else if favoritesUsersError}
              <p class="text-sm text-destructive">{favoritesUsersError}</p>
            {:else if favoritesUsers.length === 0}
              <p class="text-sm text-muted-foreground">No users available.</p>
            {:else}
              <Input
                type="text"
                class="text-sm"
                placeholder="Filter usernames..."
                bind:value={usersQuery}
              />
              <div
                class="max-h-56 overflow-y-auto rounded border border-border"
              >
                <ul class="divide-y divide-border">
                  {#if filteredUsers.length === 0}
                    <li class="px-3 py-3 text-xs text-muted-foreground">
                      No users match "{usersQuery}".
                    </li>
                  {:else}
                    {#each filteredUsers as user (`${user.username}-${user.source_count}`)}
                      <li
                        class="px-3 py-2 text-xs text-foreground flex items-center justify-between gap-3"
                      >
                        <div class="min-w-0">
                          <p class="font-mono text-xs text-foreground truncate">
                            {user.username}
                          </p>
                          <p class="text-[11px] text-muted-foreground">
                            {#if user.has_favorites}
                              favorites: {user.favorites_count}
                            {:else}
                              no favorites
                            {/if}
                            {#if user.sources.length > 0}
                              | {user.sources.join(", ")}
                            {/if}
                          </p>
                        </div>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          class="cursor-pointer h-7 px-2 text-[11px]"
                          onclick={() => copySingleUser(user.username)}
                        >
                          <Copy class="size-3" />
                        </Button>
                      </li>
                    {/each}
                  {/if}
                </ul>
              </div>
            {/if}
          </div>
        {/if}
      {/if}
    </div>
  {/if}

  <hr class="my-4" />

  <div class="space-y-2">
    <div class="flex items-center justify-between gap-2">
      <h4 class="text-sm font-medium text-foreground">
        Favorites Media Snapshot
      </h4>
      <Button
        size="sm"
        class="cursor-pointer gap-1"
        onclick={toggleFavoritesMediaPanel}
      >
        {#if favoritesMediaOpen}
          <EyeOff class="size-4" />
        {:else}
          <Eye class="size-4" />
        {/if}
        Favorites
      </Button>
    </div>
    <p class="text-xs text-muted-foreground">
      Browse all media currently seen in favorites snapshots from Emby/Jellyfin.
    </p>

    {#if favoritesMediaOpen}
      <div
        class="rounded-md border border-border bg-background/40 p-3 space-y-3"
      >
        <div class="flex flex-wrap gap-2 items-center">
          <Input
            type="text"
            class="text-sm md:flex-1"
            placeholder="Search title..."
            bind:value={favoritesMediaSearch}
            oninput={scheduleFavoritesMediaFilterReload}
          />

          <Input
            type="text"
            class="text-sm md:flex-1"
            placeholder="Filter username..."
            bind:value={favoritesMediaUsername}
            oninput={scheduleFavoritesMediaFilterReload}
          />

          <Select.Root
            type="single"
            bind:value={favoritesMediaType}
            onValueChange={() => loadFavoritesMedia(1)}
            disabled={favoritesMediaLoading}
          >
            <Select.Trigger
              class="flex-1 md:w-fit bg-card text-card-foreground"
            >
              {favoritesMediaType === "all"
                ? "All media types"
                : favoritesMediaType === MediaType.Movie
                  ? "Movies"
                  : "Series"}
            </Select.Trigger>
            <Select.Content class="bg-card">
              <Select.Item
                value="all"
                label="All media types"
                class="text-card-foreground"
              >
                All media types
              </Select.Item>
              <Select.Item
                value={MediaType.Movie}
                label="Movies"
                class="text-card-foreground"
              >
                Movies
              </Select.Item>
              <Select.Item
                value={MediaType.Series}
                label="Series"
                class="text-card-foreground"
              >
                Series
              </Select.Item>
            </Select.Content>
          </Select.Root>

          <Button
            size="icon-sm"
            class="cursor-pointer"
            onclick={() => loadFavoritesMedia(1, true)}
            disabled={favoritesMediaLoading}
          >
            <RotateCcw class="size-4" />
          </Button>
        </div>

        {#if favoritesMediaLoading}
          <span class="inline-flex gap-3 text-sm text-muted-foreground">
            <Spinner class="h-[1.2rem] w-[1.2rem] text-primary" /> Loading favorites
            media...
          </span>
        {:else if favoritesMediaError}
          <p class="text-sm text-destructive">{favoritesMediaError}</p>
        {:else if !favoritesMediaData || favoritesMediaData.items.length === 0}
          <p class="text-sm text-muted-foreground">No favorites media found.</p>
        {:else}
          <div class="space-y-2">
            {#each favoritesMediaData.items as entry (`${entry.media_type}-${entry.tmdb_id}`)}
              <div
                class="rounded-md border border-border bg-background/70 px-3 py-2"
              >
                <div class="flex gap-3">
                  <PosterThumb
                    mediaType={entry.media_type}
                    posterUrl={entry.poster_url}
                    posterSize={"92"}
                    tailWindElSize={"w-12"}
                    showMediaType={true}
                  />
                  <div class="min-w-0 flex-1 space-y-1">
                    <div class="text-sm font-medium text-foreground truncate">
                      {entry.title}{entry.year ? ` (${entry.year})` : ""}
                    </div>
                    <div class="flex flex-wrap items-center gap-1.5">
                      <span class="text-xs text-muted-foreground"
                        >Favorited by</span
                      >
                      {#each visibleFavoriteUsers(entry) as username (username)}
                        <Badge variant="outline" class="text-[11px]">
                          {username}
                        </Badge>
                      {/each}
                      {#if hiddenFavoriteUsers(entry).length > 0}
                        <Tooltip.Root>
                          <Tooltip.Trigger>
                            <Badge
                              variant="outline"
                              class="text-[11px] cursor-help"
                            >
                              +{hiddenFavoriteUsers(entry).length}
                            </Badge>
                          </Tooltip.Trigger>
                          <Tooltip.Content>
                            <p>{hiddenFavoriteUsers(entry).join(", ")}</p>
                          </Tooltip.Content>
                        </Tooltip.Root>
                      {/if}
                      <span class="text-[11px] text-muted-foreground">
                        ({entry.favorite_user_count})
                      </span>
                    </div>
                    {#if entry.is_missing_metadata}
                      <p class="text-[11px] text-amber-500">
                        Local metadata missing for TMDB {entry.tmdb_id}
                      </p>
                    {/if}
                  </div>
                </div>
              </div>
            {/each}
          </div>

          {#if favoritesMediaData.total_pages > 1}
            <div
              class="flex flex-wrap justify-center gap-2 md:flex-nowrap md:justify-between items-center"
            >
              <p class="text-xs text-muted-foreground">
                Showing {(favoritesMediaData.page - 1) *
                  favoritesMediaData.per_page +
                  1}
                to {Math.min(
                  favoritesMediaData.page * favoritesMediaData.per_page,
                  favoritesMediaData.total,
                )} of {favoritesMediaData.total}
              </p>
              <CompactPagination
                currentPage={favoritesMediaData.page}
                totalPages={favoritesMediaData.total_pages}
                maxVisiblePages={3}
                onPageChange={(page) => loadFavoritesMedia(page)}
              />
            </div>
          {/if}
        {/if}
      </div>
    {/if}
  </div>
</div>
