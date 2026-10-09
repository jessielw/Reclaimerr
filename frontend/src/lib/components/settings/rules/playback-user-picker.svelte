<script lang="ts">
  import { get_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Input } from "$lib/components/ui/input/index.js";

  interface PlaybackUserOption {
    username: string;
    /** Set only for accounts no provider could name. */
    display_name?: string | null;
    source_services: string[];
    /** Other keys this person's playback is recorded under. */
    aliases?: string[];
  }

  interface Props {
    open?: boolean;
    initialSelectedUsernames?: string[];
    onApply: (usernames: string[]) => void;
  }

  let {
    open = $bindable(false),
    initialSelectedUsernames = [],
    onApply,
  }: Props = $props();

  let query = $state("");
  let loading = $state(false);
  let error = $state<string | null>(null);
  let users = $state<PlaybackUserOption[]>([]);
  let selectedUsernames = $state<string[]>([]);

  const normalize = (value: string) => value.trim().toLowerCase();
  // rules saved before names were resolved may hold one of the person's
  // aliases instead (a Plex account number); those still match, so show them
  const userKeys = (user: PlaybackUserOption) =>
    new Set([user.username, ...(user.aliases ?? [])].map(normalize));
  const isSelected = (user: PlaybackUserOption) => {
    const keys = userKeys(user);
    return selectedUsernames.some((item) => keys.has(normalize(item)));
  };

  const loadUsers = async () => {
    if (!open) return;
    loading = true;
    error = null;
    try {
      users = await get_api<PlaybackUserOption[]>(
        "/api/rules/playback-users?limit=500",
      );
    } catch (e: any) {
      error = e.message ?? "Failed to load playback users.";
      users = [];
    } finally {
      loading = false;
    }
  };

  const filteredUsers = $derived.by(() => {
    const needle = normalize(query);
    if (!needle) return users;
    return users.filter(
      (user) =>
        [user.username, user.display_name ?? "", ...(user.aliases ?? [])].some(
          (name) => normalize(name).includes(needle),
        ) ||
        user.source_services.some((service) =>
          normalize(service).includes(needle),
        ),
    );
  });

  const toggleSelected = (user: PlaybackUserOption) => {
    if (isSelected(user)) {
      const keys = userKeys(user);
      selectedUsernames = selectedUsernames.filter(
        (item) => !keys.has(normalize(item)),
      );
      return;
    }
    selectedUsernames = [...selectedUsernames, user.username];
  };

  const applySelection = () => {
    const deduplicated = new Map<string, string>();
    for (const username of selectedUsernames) {
      const trimmed = username.trim();
      if (trimmed) deduplicated.set(normalize(trimmed), trimmed);
    }
    onApply(
      [...deduplicated.values()].sort((a, b) =>
        a.localeCompare(b, undefined, { sensitivity: "base" }),
      ),
    );
    open = false;
  };

  $effect(() => {
    if (!open) return;
    selectedUsernames = initialSelectedUsernames
      .map((item) => item.trim())
      .filter(Boolean);
    query = "";
    void loadUsers();
  });
</script>

<Dialog.Root bind:open>
  <Dialog.Content
    class="sm:max-w-2xl h-[min(90vh,44rem)] max-h-[90vh] p-0 flex flex-col overflow-hidden border-ring border-2 text-foreground"
    onInteractOutside={(event) => event.preventDefault()}
  >
    <Dialog.Header class="px-6 pt-5 pb-3 shrink-0 border-b border-border">
      <Dialog.Title>Select Playback Users</Dialog.Title>
      <Dialog.Description>
        Select usernames resolved from retained playback history. Manually typed
        usernames are preserved.
      </Dialog.Description>
    </Dialog.Header>

    <div class="flex-1 overflow-y-auto px-6 py-4 space-y-4 min-h-0">
      <Input
        type="text"
        placeholder="Search by name, account or source service..."
        value={query}
        oninput={(event) => (query = event.currentTarget.value)}
      />

      <div class="rounded-md border border-border overflow-y-auto min-h-0">
        {#if loading}
          <p class="p-3 text-sm text-muted-foreground">Loading users...</p>
        {:else if error}
          <p class="p-3 text-sm text-destructive">{error}</p>
        {:else if users.length === 0}
          <div class="p-3 space-y-1 text-muted-foreground">
            <p class="text-sm">No resolved playback users found.</p>
            <p class="text-xs">
              Refresh playback data or enter usernames manually in the rule.
            </p>
          </div>
        {:else if filteredUsers.length === 0}
          <p class="p-3 text-sm text-muted-foreground">
            No users match "{query}".
          </p>
        {:else}
          <ul class="divide-y divide-border">
            {#each filteredUsers as user (normalize(user.username))}
              <li class="flex items-center gap-3 px-3 py-2">
                <input
                  type="checkbox"
                  class="size-4"
                  checked={isSelected(user)}
                  oninput={() => toggleSelected(user)}
                />
                <button
                  type="button"
                  class="flex-1 text-left"
                  onclick={() => toggleSelected(user)}
                >
                  <p
                    class="text-sm"
                    class:text-muted-foreground={user.display_name}
                  >
                    {user.display_name ?? user.username}
                  </p>
                  <p class="text-xs text-muted-foreground">
                    {user.source_services.join(", ")}
                    {#if user.aliases?.length}
                      · also recorded as {user.aliases.join(", ")}
                    {/if}
                  </p>
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    </div>

    <Dialog.Footer class="px-6 py-4 shrink-0 border-t border-border mt-auto">
      <Button variant="secondary" onclick={() => (open = false)}>Cancel</Button>
      <Button onclick={applySelection}>Apply Users</Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
