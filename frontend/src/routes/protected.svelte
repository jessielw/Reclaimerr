<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { get_api, delete_api, put_api } from "$lib/api";
  import ErrorBox from "$lib/components/error-box.svelte";
  import CompactPagination from "$lib/components/compact-pagination.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import { auth } from "$lib/stores/auth";
  import {
    MediaType,
    Permission,
    UserRole,
    type ProtectedEntry,
    type PaginatedResponse,
  } from "$lib/types/shared";
  import { formatDate, formatDistanceToNow } from "$lib/utils/date";
  import {
    createPerPageState,
    createFilterState,
    PER_PAGE_OPTIONS,
  } from "$lib/utils/pagination";
  import { toast } from "svelte-sonner";
  import Search from "@lucide/svelte/icons/search";
  import Pencil from "@lucide/svelte/icons/pencil";
  import Trash from "@lucide/svelte/icons/trash";
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";

  // state
  let data = $state<PaginatedResponse<ProtectedEntry> | null>(null);
  let loading = $state(true);
  let error = $state("");
  let searchQuery = $state("");
  const _sortByStore = createFilterState("protected_sort_by", "created_at");
  const _sortOrderStore = createFilterState("protected_sort_order", "desc");
  const _mediaTypeStore = createFilterState("protected_media_type", "all");
  let sortBy = $state(_sortByStore.getInitial());
  let sortOrder = $state(_sortOrderStore.getInitial());
  let mediaTypeFilter = $state(_mediaTypeStore.getInitial());
  let currentPage = $state(1);
  const _perPageStore = createPerPageState("protected_per_page");
  let perPage = $state(_perPageStore.getInitial());
  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  let abortController: AbortController | null = null;
  let mounted = $state(false);

  // sorting options
  const sortByOptions = [
    { value: "created_at", label: "Date Added" },
    { value: "media_title", label: "Title" },
    { value: "expires_at", label: "Protection End" },
  ];

  // edit duration dialog state
  let editDialogOpen = $state(false);
  let editTarget = $state<ProtectedEntry | null>(null);
  let editDuration = $state("permanent");
  let editCustomDays = $state("30");
  let editSubmitting = $state(false);

  // remove confirmation state
  let removeDialogOpen = $state(false);
  let removeTarget = $state<ProtectedEntry | null>(null);
  let removeSubmitting = $state(false);

  const entries = $derived(data?.items ?? []);

  const canManageProtection = $derived.by(() => {
    const permissions = $auth.user?.permissions ?? [];
    return (
      $auth.user?.role === UserRole.Admin ||
      permissions.includes(Permission.ManageProtection)
    );
  });

  $effect(() => _sortByStore.save(sortBy));
  $effect(() => _sortOrderStore.save(sortOrder));
  $effect(() => _mediaTypeStore.save(mediaTypeFilter));

  $effect(() => {
    sortBy;
    sortOrder;
    mediaTypeFilter;
    perPage;
    if (mounted) {
      loadProtectedEntries(1);
    }
  });

  // load protected entries with current filters and pagination
  const loadProtectedEntries = async (page: number = currentPage) => {
    if (abortController) abortController.abort();
    abortController = new AbortController();
    const signal = abortController.signal;

    loading = true;
    error = "";
    currentPage = page;

    try {
      const params = new URLSearchParams({
        page: page.toString(),
        per_page: perPage.toString(),
        sort_by: sortBy,
        sort_order: sortOrder,
      });

      if (searchQuery.trim()) {
        params.append("search", searchQuery.trim());
      }

      if (mediaTypeFilter !== "all") {
        params.append("media_type", mediaTypeFilter);
      }

      data = await get_api<PaginatedResponse<ProtectedEntry>>(
        `/api/protected?${params.toString()}`,
        signal,
      );
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      error = e.message ?? "Failed to load protected entries.";
    } finally {
      if (!signal.aborted) {
        loading = false;
      }
    }
  };

  // handle search input with debounce
  const handleSearch = (event: Event) => {
    const target = event.target as HTMLInputElement;
    searchQuery = target.value;

    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      loadProtectedEntries(1);
    }, 400);
  };

  // open edit duration dialog for a specific entry
  const openEditDuration = (entry: ProtectedEntry) => {
    editTarget = entry;
    editDuration = entry.permanent ? "permanent" : "30";
    editCustomDays = "30";
    editDialogOpen = true;
  };

  // update the specific entry in the current data with the updated entry from API
  const patchEntry = (updatedEntry: ProtectedEntry) => {
    if (!data) return;

    data = {
      ...data,
      items: data.items.map((item) =>
        item.id === updatedEntry.id ? updatedEntry : item,
      ),
    };
  };

  // submit updated duration to API
  const submitEditDuration = async () => {
    if (!editTarget) return;
    editSubmitting = true;

    let durationDays: number | null = null;
    if (editDuration === "permanent") {
      durationDays = null;
    } else if (editDuration === "custom") {
      const customDays = Number(editCustomDays);
      if (!Number.isInteger(customDays) || customDays <= 0) {
        toast.error("Duration must be a positive whole number of days.");
        editSubmitting = false;
        return;
      }
      durationDays = customDays;
    } else {
      durationDays = Number(editDuration);
      if (!Number.isInteger(durationDays) || durationDays <= 0) {
        toast.error("Duration must be a positive whole number of days.");
        editSubmitting = false;
        return;
      }
    }

    try {
      const updatedEntry = await put_api<ProtectedEntry>(
        `/api/protected/${editTarget.id}/duration`,
        {
          duration_days: durationDays,
        },
      );
      toast.success("Protection duration updated.");
      patchEntry(updatedEntry);
      editDialogOpen = false;
      editTarget = null;
    } catch (e: any) {
      toast.error(e.message ?? "Failed to update duration.");
    } finally {
      editSubmitting = false;
    }
  };

  // open remove confirmation dialog for a specific entry
  const openRemoveDialog = (entry: ProtectedEntry) => {
    removeTarget = entry;
    removeDialogOpen = true;
  };

  // remove the entry from protection list via API and update local data accordingly
  const removeEntry = async () => {
    if (!removeTarget) return;
    removeSubmitting = true;

    try {
      await delete_api(`/api/protected/${removeTarget.id}`);
      toast.success("Protection removed.");
      removeDialogOpen = false;

      if (!data) {
        await loadProtectedEntries(currentPage);
        return;
      }

      const remainingItems = data.items.filter(
        (item) => item.id !== removeTarget?.id,
      );
      const newTotal = Math.max(0, data.total - 1);
      const newTotalPages =
        newTotal > 0 ? Math.ceil(newTotal / data.per_page) : 0;

      if (remainingItems.length === 0 && currentPage > 1) {
        await loadProtectedEntries(currentPage - 1);
        return;
      }

      data = {
        ...data,
        items: remainingItems,
        total: newTotal,
        total_pages: newTotalPages,
      };
      removeTarget = null;
    } catch (e: any) {
      toast.error(e.message ?? "Failed to remove protection.");
    } finally {
      removeSubmitting = false;
    }
  };

  onMount(async () => {
    mounted = true;
    await loadProtectedEntries();
  });

  onDestroy(() => {
    if (searchTimer) clearTimeout(searchTimer);
    if (abortController) abortController.abort();
  });
</script>

<!-- edit duration dialog -->
<Dialog.Root bind:open={editDialogOpen}>
  <Dialog.Content class="sm:max-w-md text-foreground">
    <Dialog.Header>
      <Dialog.Title>Edit Protection Duration</Dialog.Title>
      <Dialog.Description>
        {#if editTarget}
          Update protection for <strong>{editTarget.media_title}</strong>.
        {/if}
      </Dialog.Description>
    </Dialog.Header>

    <div class="flex flex-col gap-4 py-2">
      <div class="flex flex-col gap-2">
        <Label for="duration-select">Protection Duration</Label>
        <Select.Root type="single" bind:value={editDuration}>
          <Select.Trigger
            class="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            {(() => {
              if (editDuration === "permanent") return "Permanent";
              if (editDuration === "custom") return "Custom days";
              if (!isNaN(Number(editDuration))) return `${editDuration} days`;
              return editDuration;
            })()}
          </Select.Trigger>
          <Select.Content class="bg-card border-ring">
            <Select.Item value="30" label="30 days" class="text-foreground"
              >30 days</Select.Item
            >
            <Select.Item value="90" label="90 days" class="text-foreground"
              >90 days</Select.Item
            >
            <Select.Item value="180" label="180 days" class="text-foreground"
              >180 days</Select.Item
            >
            <Select.Item value="365" label="1 year" class="text-foreground"
              >1 year</Select.Item
            >
            <Select.Item
              value="custom"
              label="Custom days"
              class="text-foreground">Custom days</Select.Item
            >
            <Select.Item
              value="permanent"
              label="Permanent"
              class="text-foreground">Permanent</Select.Item
            >
          </Select.Content>
        </Select.Root>

        {#if editDuration === "custom"}
          <Input
            id="duration-select"
            type="number"
            min="1"
            step="1"
            bind:value={editCustomDays}
            placeholder="Enter number of days"
            class="input-hover-el"
          />
        {/if}
      </div>
    </div>

    <Dialog.Footer>
      <Button variant="outline" onclick={() => (editDialogOpen = false)}>
        Cancel
      </Button>
      <Button onclick={submitEditDuration} disabled={editSubmitting}>
        Save
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

<!-- remove confirmation dialog -->
<AlertDialog.Root bind:open={removeDialogOpen}>
  <AlertDialog.Content
    class="bg-card border border-border rounded-lg p-6 max-w-md w-full text-foreground"
  >
    <AlertDialog.Header>
      <AlertDialog.Title>Remove Protection</AlertDialog.Title>
      <AlertDialog.Description>
        {#if removeTarget}
          Remove protection for <strong>{removeTarget.media_title}</strong>?
          This action cannot be undone.
        {/if}
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer class="flex justify-end gap-3 pt-4">
      <AlertDialog.Cancel
        class="cursor-pointer hover text-foreground bg-secondary"
      >
        Cancel
      </AlertDialog.Cancel>
      <AlertDialog.Action
        class="cursor-pointer hover"
        onclick={removeEntry}
        disabled={removeSubmitting}
      >
        Remove
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<div class="p-2.5 md:p-8 max-w-7xl mx-auto space-y-6">
  <div class="space-y-2">
    <h1 class="text-3xl font-bold text-foreground">Protected</h1>
    <p class="text-muted-foreground">
      Media items protected from cleanup and deletion.
      {#if canManageProtection}
        You can also remove protection from items here.
      {/if}
    </p>
  </div>

  <div class="mb-4 flex flex-col sm:flex-row gap-2">
    <div class="relative flex-1">
      <Search
        class="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted-foreground"
      />
      <Input
        type="text"
        placeholder="Search by media title, reason, or user"
        value={searchQuery}
        oninput={handleSearch}
        class="pl-10 bg-card"
      />
    </div>

    <div class="flex flex-1 flex-col gap-2 sm:flex-row">
      <!-- row 1 on mobile: sort by + sort order -->
      <div class="flex flex-1 gap-2">
        <!-- sort by -->
        <Select.Root type="single" bind:value={sortBy}>
          <Select.Trigger class="flex-1 bg-card text-card-foreground">
            {sortByOptions.find((opt) => opt.value === sortBy)?.label}
          </Select.Trigger>
          <Select.Content class="bg-card">
            {#each sortByOptions as option}
              <Select.Item
                value={option.value}
                label={option.label}
                class="text-card-foreground"
              >
                {option.label}
              </Select.Item>
            {/each}
          </Select.Content>
        </Select.Root>

        <!-- sort order -->
        <Select.Root type="single" bind:value={sortOrder}>
          <Select.Trigger class="flex-1 bg-card text-card-foreground">
            {sortOrder === "asc" ? "Ascending" : "Descending"}
          </Select.Trigger>
          <Select.Content class="bg-card">
            <Select.Item
              value="asc"
              label="Ascending"
              class="text-card-foreground">Ascending</Select.Item
            >
            <Select.Item
              value="desc"
              label="Descending"
              class="text-card-foreground">Descending</Select.Item
            >
          </Select.Content>
        </Select.Root>
      </div>

      <!-- row 2 on mobile: media type + per page -->
      <div class="flex flex-1 gap-2">
        <!-- media type filter -->
        <Select.Root type="single" bind:value={mediaTypeFilter}>
          <Select.Trigger class="flex-1 bg-card text-card-foreground">
            {mediaTypeFilter === "all"
              ? "All Media"
              : mediaTypeFilter === MediaType.Movie
                ? "Movies"
                : "Series"}
          </Select.Trigger>
          <Select.Content class="bg-card">
            <Select.Item
              value="all"
              label="All Media"
              class="text-card-foreground">All Media</Select.Item
            >
            <Select.Item
              value={MediaType.Movie}
              label="Movies"
              class="text-card-foreground">Movies</Select.Item
            >
            <Select.Item
              value={MediaType.Series}
              label="Series"
              class="text-card-foreground">Series</Select.Item
            >
          </Select.Content>
        </Select.Root>

        <!-- per page -->
        <Select.Root
          type="single"
          value={perPage.toString()}
          onValueChange={(v) => {
            const n = parseInt(v, 10);
            if (!isNaN(n)) {
              perPage = n;
              _perPageStore.save(n);
            }
          }}
        >
          <Select.Trigger class="flex-1 bg-card text-card-foreground">
            {perPage} / page
          </Select.Trigger>
          <Select.Content class="bg-card">
            {#each PER_PAGE_OPTIONS as opt}
              <Select.Item
                value={opt.toString()}
                label={`${opt} / page`}
                class="text-card-foreground"
              >
                {opt} / page
              </Select.Item>
            {/each}
          </Select.Content>
        </Select.Root>
      </div>
    </div>
  </div>

  <!-- error box -->
  <ErrorBox {error} />

  <div class="bg-card rounded-lg border border-border overflow-x-auto">
    {#if loading}
      <div class="p-8 text-center text-muted-foreground">
        <div
          class="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-primary
            border-r-transparent"
        ></div>
        <p class="mt-4">Loading protected items...</p>
      </div>
    {:else if entries.length === 0}
      <div class="p-8 text-center text-muted-foreground">
        No protected items found.
      </div>
    {:else}
      <table class="w-full">
        <thead class="bg-muted/50">
          <tr>
            <th
              class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
              >Media</th
            >
            <th
              class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
              >Protected Until</th
            >
            <th
              class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
              >Added By</th
            >
            <th
              class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
              >Created</th
            >
            {#if canManageProtection}
              <th
                class="px-6 py-3 text-right text-xs font-medium text-muted-foreground uppercase tracking-wider"
                >Actions</th
              >
            {/if}
          </tr>
        </thead>
        <tbody class="divide-y divide-border">
          {#each entries as entry (entry.id)}
            <tr class="hover:bg-muted/30 transition-colors">
              <td class="px-6 py-4">
                <div class="flex gap-4">
                  <div class="flex flex-col items-center w-max gap-1">
                    <PosterThumb
                      mediaType={entry.media_type}
                      posterUrl={entry.poster_url}
                    />
                    <MediaTypeBadge mediaType={entry.media_type} />
                  </div>
                  <div>
                    <div class="text-sm font-medium text-foreground">
                      {entry.media_title}{entry.media_year ? ` (${entry.media_year})` : ''}
                    </div>
                  </div>
                </div>
                {#if entry.reason}
                  <div class="text-xs text-muted-foreground mt-1">
                    {entry.reason}
                  </div>
                {/if}
              </td>
              <td class="px-6 py-4 text-sm text-foreground whitespace-nowrap">
                {#if entry.permanent}
                  Permanent
                {:else if entry.expires_at}
                  <div>{formatDate(entry.expires_at)}</div>
                  <div class="text-xs text-muted-foreground">
                    {formatDistanceToNow(entry.expires_at)}
                  </div>
                {:else}
                  Unknown
                {/if}
              </td>
              <td class="px-6 py-4 text-sm text-foreground whitespace-nowrap">
                {entry.protected_by_username}
              </td>
              <td
                class="px-6 py-4 text-sm text-muted-foreground whitespace-nowrap"
              >
                {formatDate(entry.created_at)}
              </td>
              {#if canManageProtection}
                <td class="px-6 py-4 text-right whitespace-nowrap">
                  <div class="flex justify-end gap-2">
                    <Button
                      type="button"
                      size="icon"
                      class="rounded-full cursor-pointer"
                      onclick={() => openEditDuration(entry)}
                    >
                      <Pencil class="w-4 h-4" />
                    </Button>
                    <Button
                      type="button"
                      size="icon"
                      class="rounded-full cursor-pointer hover:bg-destructive-secondary 
                        bg-destructive text-destructive-foreground"
                      onclick={() => openRemoveDialog(entry)}
                    >
                      <Trash class="w-4 h-4" />
                    </Button>
                  </div>
                </td>
              {/if}
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>

  {#if !loading && entries.length !== 0 && data && data.total_pages > 1}
    <div
      class="flex flex-wrap justify-center gap-2 md:flex-nowrap md:justify-between items-center"
    >
      <p class="text-sm text-muted-foreground">
        Showing {(data.page - 1) * data.per_page + 1} to {Math.min(
          data.page * data.per_page,
          data.total,
        )} of {data.total} entries
      </p>

      <CompactPagination
        currentPage={data.page}
        totalPages={data.total_pages}
        maxVisiblePages={3}
        onPageChange={loadProtectedEntries}
      />
    </div>
  {/if}
</div>
