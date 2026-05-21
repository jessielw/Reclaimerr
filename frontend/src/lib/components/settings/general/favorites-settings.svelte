<script lang="ts">
  import { get_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Eye from "@lucide/svelte/icons/eye";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
  import Copy from "@lucide/svelte/icons/copy";
  import UserStar from "@lucide/svelte/icons/user-star";
  import { toast } from "svelte-sonner";

  interface FavoritesUserLookup {
    username: string;
    has_favorites: boolean;
    favorites_count: number;
    source_count: number;
    sources: string[];
  }

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

  const copySingleUser = async (username: string) => {
    try {
      await navigator.clipboard.writeText(username);
      toast.success(`Copied ${username}.`);
    } catch {
      toast.error("Failed to copy username.");
    }
  };
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
              <Eye class="size-4" />
              {favoritesUsersOpen ? "Hide users" : "Show users"}
            </Button>
          </div>
          <textarea
            id="favoritesUsernames"
            class="input-hover-el min-h-20 w-full rounded-md border border-input bg-background
              px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground"
            placeholder="user1, user2"
            bind:value={favoritesUsernamesInput}
            disabled={!favoritesProtectAllUsers}
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
</div>
