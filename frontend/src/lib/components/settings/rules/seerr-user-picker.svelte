<script lang="ts">
  import { get_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Input } from "$lib/components/ui/input/index.js";

  interface SeerrUserOption {
    id: number;
    username: string | null;
    display_name: string | null;
  }

  interface Props {
    open?: boolean;
    initialSelectedIds?: string[];
    onApply: (ids: string[]) => void;
  }

  let {
    open = $bindable(false),
    initialSelectedIds = [],
    onApply,
  }: Props = $props();

  let query = $state("");
  let loading = $state(false);
  let error = $state<string | null>(null);
  let users = $state<SeerrUserOption[]>([]);
  let selectedIds = $state<string[]>([]);

  const loadUsers = async () => {
    if (!open) return;
    loading = true;
    error = null;
    try {
      const params = new URLSearchParams();
      params.set("limit", "100");
      users = await get_api<SeerrUserOption[]>(
        `/api/rules/seerr-users?${params.toString()}`,
      );
    } catch (e: any) {
      error = e.message ?? "Failed to load Seerr users.";
      users = [];
    } finally {
      loading = false;
    }
  };

  const filteredUsers = $derived.by(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return users;
    return users.filter(
      (user) =>
        String(user.id).includes(needle) ||
        (user.username || "").toLowerCase().includes(needle) ||
        (user.display_name || "").toLowerCase().includes(needle),
    );
  });

  const isSelected = (id: number) => selectedIds.includes(String(id));

  const toggleSelected = (id: number) => {
    const key = String(id);
    if (selectedIds.includes(key)) {
      selectedIds = selectedIds.filter((item) => item !== key);
      return;
    }
    selectedIds = [...selectedIds, key];
  };

  const applySelection = () => {
    onApply([...new Set(selectedIds)].sort((a, b) => Number(a) - Number(b)));
    open = false;
  };

  $effect(() => {
    if (!open) return;
    selectedIds = [
      ...new Set(initialSelectedIds.map((item) => item.trim()).filter(Boolean)),
    ];
    query = "";
    void loadUsers();
  });
</script>

<Dialog.Root bind:open>
  <Dialog.Content
    class="sm:max-w-2xl h-[min(90vh,44rem)] max-h-[90vh] p-0 flex flex-col overflow-hidden border-ring border-2
      text-foreground"
    onInteractOutside={(e) => {
      e.preventDefault();
    }}
  >
    <Dialog.Header class="px-6 pt-5 pb-3 shrink-0 border-b border-border">
      <Dialog.Title>Select Seerr Requesters</Dialog.Title>
      <Dialog.Description>
        Search Seerr users and add requester IDs to this rule condition.
      </Dialog.Description>
    </Dialog.Header>

    <div class="flex-1 overflow-y-auto px-6 py-4 space-y-4 min-h-0">
      <Input
        type="text"
        placeholder="Search by name, username, or ID..."
        value={query}
        oninput={(e) => (query = e.currentTarget.value)}
      />

      <div class="rounded-md border border-border overflow-y-auto min-h-0">
        {#if loading}
          <p class="p-3 text-sm text-muted-foreground">Loading users...</p>
        {:else if error}
          <p class="p-3 text-sm text-destructive">{error}</p>
        {:else if users.length === 0}
          <div class="p-3 space-y-1">
            <p class="text-sm text-muted-foreground">No Seerr users found.</p>
            <p class="text-xs text-muted-foreground">
              This usually means the Seerr API key cannot list users, or Seerr
              has no requester data yet.
            </p>
            <p class="text-xs text-muted-foreground">
              You can still type requester IDs manually in the rule value field
              (comma-separated).
            </p>
          </div>
        {:else if filteredUsers.length === 0}
          <p class="p-3 text-sm text-muted-foreground">
            No users match "{query}".
          </p>
        {:else}
          <ul class="divide-y divide-border">
            {#each filteredUsers as user (`${user.id}`)}
              <li class="flex items-center gap-3 px-3 py-2">
                <input
                  type="checkbox"
                  class="size-4"
                  checked={isSelected(user.id)}
                  oninput={() => toggleSelected(user.id)}
                />
                <button
                  type="button"
                  class="flex-1 text-left"
                  onclick={() => toggleSelected(user.id)}
                >
                  <p class="text-sm">
                    {user.display_name || user.username || `User ${user.id}`}
                  </p>
                  <p class="text-xs text-muted-foreground">
                    ID {user.id}
                    {#if user.username}
                      | @{user.username}
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
      <Button onclick={applySelection}>Apply IDs</Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
