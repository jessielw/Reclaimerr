<script lang="ts">
  import { get_api, post_api } from "$lib/api";
  import { auth } from "$lib/stores/auth";
  import { uiIndicators } from "$lib/stores/ui-indicators";
  import type { AdminNotice, AdminNoticesResponse } from "$lib/types/shared";
  import { formatDateTimeToLocaleString } from "$lib/utils/date";
  import Bell from "@lucide/svelte/icons/bell";
  import BellDot from "@lucide/svelte/icons/bell-dot";
  import * as Dialog from "$lib/components/ui/dialog/index.js";

  let noticesDialogOpen = $state(false);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let unreadCount = $state(0);
  let notices = $state<AdminNotice[]>([]);

  const loadNotices = async () => {
    if ($auth.user?.role !== "admin") return;
    loading = true;
    error = null;
    try {
      const payload = await get_api<AdminNoticesResponse>(
        "/api/info/notices?limit=100",
      );
      unreadCount = payload.unread_count;
      notices = payload.items;
    } catch (e: any) {
      error = e?.message ?? "Failed to load notices.";
      notices = [];
      unreadCount = 0;
    } finally {
      loading = false;
      void uiIndicators.refreshNow();
    }
  };

  const markReadState = async (notice: AdminNotice, read: boolean) => {
    try {
      await post_api(
        `/api/info/notices/${notice.id}/${read ? "read" : "unread"}`,
        {},
      );
      await loadNotices();
    } catch (e: any) {
      error = e?.message ?? "Failed to update notice state.";
    }
  };

  const isExternalHref = (href: string | null) =>
    !!href && /^https?:\/\//i.test(href);

  $effect(() => {
    if (!noticesDialogOpen) return;
    void loadNotices();
  });
</script>

{#if $auth.user?.role === "admin"}
  <button
    type="button"
    class="relative inline-flex items-center justify-center rounded-md p-1
      hover:bg-accent hover:text-accent-foreground cursor-pointer"
    aria-label="Open notices"
    title={unreadCount > 0 ? `${unreadCount} unread notice(s)` : "Notices"}
    onclick={() => (noticesDialogOpen = true)}
  >
    {#if $uiIndicators.hasUnreadNotices}
      <BellDot class="size-4.5 text-green-500" />
    {:else}
      <Bell class="size-4.5 text-muted-foreground/35" />
    {/if}
  </button>

  <Dialog.Root bind:open={noticesDialogOpen}>
    <Dialog.Content class="sm:max-w-lg text-foreground border-ring border-2">
      <Dialog.Header>
        <Dialog.Title>Notices</Dialog.Title>
      </Dialog.Header>

      <div class="space-y-3 py-2 max-h-[65vh] overflow-y-auto pr-1">
        {#if loading}
          <p class="text-sm text-muted-foreground">Loading notices...</p>
        {:else if error}
          <p class="text-sm text-destructive">{error}</p>
        {:else if notices.length === 0}
          <p class="text-sm text-muted-foreground">No notices yet.</p>
        {:else}
          {#each notices as notice (`${notice.id}`)}
            <div
              class="rounded-md border border-border p-3 space-y-2
                {notice.is_read ? 'opacity-80' : ''}"
            >
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <p class="text-sm font-semibold">{notice.title}</p>
                  <p class="text-xs text-muted-foreground mt-0.5">
                    {notice.last_occurred_at
                      ? formatDateTimeToLocaleString(notice.last_occurred_at)
                      : formatDateTimeToLocaleString(notice.updated_at)}
                  </p>
                </div>
                <span
                  class="text-[11px] px-2 py-0.5 rounded-full
                    {notice.is_read
                    ? 'bg-muted text-muted-foreground'
                    : 'bg-green-100 text-green-700'}"
                >
                  {notice.is_read ? "Read" : "Unread"}
                </span>
              </div>
              <p class="text-sm text-foreground/90 whitespace-pre-wrap">
                {notice.message}
              </p>
              <div class="flex items-center gap-3">
                {#if notice.action_href && notice.action_label}
                  <a
                    href={notice.action_href}
                    class="text-xs text-primary hover:underline"
                    target={isExternalHref(notice.action_href)
                      ? "_blank"
                      : undefined}
                    rel={isExternalHref(notice.action_href)
                      ? "noopener noreferrer"
                      : undefined}
                    onclick={() => (noticesDialogOpen = false)}
                  >
                    {notice.action_label}
                  </a>
                {/if}
                <button
                  type="button"
                  class="text-xs text-muted-foreground hover:text-foreground cursor-pointer"
                  onclick={() => markReadState(notice, !notice.is_read)}
                >
                  {notice.is_read ? "Mark unread" : "Mark read"}
                </button>
              </div>
            </div>
          {/each}
        {/if}
      </div>
    </Dialog.Content>
  </Dialog.Root>
{/if}
