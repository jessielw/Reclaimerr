<script lang="ts">
  import { onMount } from "svelte";
  import type { Component } from "svelte";
  import { get_api, post_api, delete_api } from "$lib/api";
  import { NotificationType } from "$lib/types/shared";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import Plus from "@lucide/svelte/icons/plus";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import TestTube from "@lucide/svelte/icons/test-tube";
  import Save from "@lucide/svelte/icons/save";
  import ChevronDown from "@lucide/svelte/icons/chevron-down";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import Pencil from "@lucide/svelte/icons/pencil";
  import Check from "@lucide/svelte/icons/check";
  import X from "@lucide/svelte/icons/x";
  import { toast } from "svelte-sonner";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Textarea } from "$lib/components/ui/textarea/index.js";

  interface Props {
    userRole: string;
    svgIcon: Component | null;
  }
  let { userRole, svgIcon }: Props = $props();

  interface NotificationConfig {
    id: number;
    enabled: boolean;
    name: string;
    url: string;
    newCleanupCandidates: boolean;
    requestApproved: boolean;
    requestDeclined: boolean;
    adminMessage: boolean;
    taskFailure: boolean;
  }

  let loading = $state(false);
  let saving = $state(false);
  let testingIndex = $state<number | null>(null);
  let notifications = $state<NotificationConfig[]>([]);
  let expandedStates = $state<Record<number, boolean>>({});
  let editingTitle = $state<number | null>(null);
  let editedTitleValue = $state("");
  let isAdmin = $derived(userRole === "admin");

  // map enum values to camelCase property keys
  type NotificationKey = keyof Omit<
    NotificationConfig,
    "id" | "enabled" | "name" | "url"
  >;

  const notificationTypeMap: Record<NotificationType, NotificationKey> = {
    [NotificationType.NewCleanupCandidates]: "newCleanupCandidates",
    [NotificationType.RequestApproved]: "requestApproved",
    [NotificationType.RequestDeclined]: "requestDeclined",
    [NotificationType.AdminMessage]: "adminMessage",
    [NotificationType.TaskFailure]: "taskFailure",
  };

  // notification type metadata
  const notificationTypes = [
    {
      type: NotificationType.NewCleanupCandidates,
      label: "New Cleanup Candidates",
      description: "Notified when new media is marked for potential cleanup",
      adminOnly: false,
    },
    {
      type: NotificationType.RequestApproved,
      label: "Request Approved",
      description: "Notified when your media requests are approved",
      adminOnly: false,
    },
    {
      type: NotificationType.RequestDeclined,
      label: "Request Declined",
      description: "Notified when your media requests are declined",
      adminOnly: false,
    },
    {
      type: NotificationType.AdminMessage,
      label: "Admin Messages",
      description: "Receive messages from administrators",
      adminOnly: false,
    },
    {
      type: NotificationType.TaskFailure,
      label: "Task Failures",
      description: "Notified when scheduled tasks fail (Admin only)",
      adminOnly: true,
    },
  ];

  // load existing notifications from API
  async function loadNotifications() {
    try {
      loading = true;
      const data = await get_api<
        Array<{
          id: number;
          enabled: boolean;
          name: string | null;
          url: string;
          new_cleanup_candidates: boolean;
          request_approved: boolean;
          request_declined: boolean;
          admin_message: boolean;
          task_failure: boolean;
        }>
      >("/api/settings/notifications");

      notifications = data.map((n) => ({
        id: n.id,
        enabled: n.enabled,
        name: n.name || "",
        url: n.url,
        newCleanupCandidates: n.new_cleanup_candidates,
        requestApproved: n.request_approved,
        requestDeclined: n.request_declined,
        adminMessage: n.admin_message,
        taskFailure: n.task_failure,
      }));
    } catch (err: any) {
      toast.error(`Failed to load notifications: ${err.message}`);
    } finally {
      loading = false;
    }
  }

  // add new notification with default values
  function addNotification() {
    const newId = Date.now() * -1; // generate a unique negative ID for new notifications
    notifications = [
      ...notifications,
      {
        id: newId,
        enabled: false,
        name: "",
        url: "",
        newCleanupCandidates: false,
        requestApproved: false,
        requestDeclined: false,
        adminMessage: false,
        taskFailure: false,
      },
    ];
    expandedStates[newId] = true; // auto-expand new notification
  }

  // save notification to API (create or update)
  async function saveNotification(index: number) {
    const notification = notifications[index];
    if (!notification.url.trim()) {
      toast.error("Apprise URL is required");
      return;
    }

    try {
      saving = true;
      const payload = {
        id: notification.id > 0 ? notification.id : undefined,
        enabled: notification.enabled,
        name: notification.name.trim() || null,
        url: notification.url,
        new_cleanup_candidates: notification.newCleanupCandidates,
        request_approved: notification.requestApproved,
        request_declined: notification.requestDeclined,
        admin_message: notification.adminMessage,
        task_failure: notification.taskFailure,
      };

      const response = await post_api<{
        message: string;
        data: {
          id: number;
          enabled: boolean;
          name: string | null;
          url: string;
          new_cleanup_candidates: boolean;
          request_approved: boolean;
          request_declined: boolean;
          admin_message: boolean;
          task_failure: boolean;
        };
      }>("/api/settings/notifications", payload);

      // update the notification with the returned data
      const oldId = notifications[index].id;
      notifications[index] = {
        id: response.data.id,
        enabled: response.data.enabled,
        name: response.data.name || "",
        url: response.data.url,
        newCleanupCandidates: response.data.new_cleanup_candidates,
        requestApproved: response.data.request_approved,
        requestDeclined: response.data.request_declined,
        adminMessage: response.data.admin_message,
        taskFailure: response.data.task_failure,
      };

      // update expanded state if ID changed (new notification saved)
      if (oldId !== response.data.id) {
        delete expandedStates[oldId];
        expandedStates[response.data.id] = expandedStates[oldId] ?? true;
      }

      toast.success(response.message);
    } catch (err: any) {
      toast.error(`Failed to save notification: ${err.message}`);
    } finally {
      saving = false;
    }
  }

  // delete notification
  async function deleteNotification(index: number) {
    const notification = notifications[index];
    if (notification.id < 0) {
      // just remove from list if not saved yet
      notifications = notifications.filter((_, i) => i !== index);
      return;
    }

    try {
      saving = true;
      await delete_api(`/api/settings/notifications/${notification.id}`);
      notifications = notifications.filter((_, i) => i !== index);
      delete expandedStates[notification.id];
      toast.success("Notification deleted successfully");
    } catch (err: any) {
      toast.error(`Failed to delete notification: ${err.message}`);
    } finally {
      saving = false;
    }
  }

  // toggle notification type on/off
  function toggleNotificationType(index: number, type: NotificationType) {
    const key = notificationTypeMap[type];
    notifications[index][key] = !notifications[index][key];
  }

  // toggle collapsible section
  function toggleExpanded(id: number) {
    expandedStates[id] = !expandedStates[id];
  }

  // edit title
  function startEditingTitle(index: number) {
    editingTitle = index;
    editedTitleValue = notifications[index].name;
  }

  // save edited title
  function saveTitle(index: number) {
    notifications[index].name = editedTitleValue.trim();
    editingTitle = null;
  }

  // cancel editing title
  function cancelEditTitle() {
    editingTitle = null;
    editedTitleValue = "";
  }

  // test notification
  async function testNotification(index: number) {
    const notification = notifications[index];
    if (!notification.url.trim()) {
      toast.error("Apprise URL is required to test");
      return;
    }

    try {
      testingIndex = index;
      await post_api("/api/settings/notifications/test", {
        url: notification.url,
      });
      toast.success("Test notification sent successfully!");
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      testingIndex = null;
    }
  }

  // load notifications on component mount
  onMount(() => {
    loadNotifications();
  });
</script>

<div class="space-y-6">
  <div class="mb-6">
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {console.log(svgIcon)}
      {#if svgIcon}
        {@const Icon = svgIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      <span class="align-middle">Notifications</span>
    </h2>
    <p class="mt-1 text-sm text-muted-foreground">
      Configure Apprise notification services to receive alerts about media
      cleanup and requests
    </p>
  </div>

  {#if loading}
    <div
      class="flex p-8 text-center items-center justify-center text-muted-foreground gap-3"
    >
      <Spinner class="size-5" />
      Loading notifications...
    </div>
  {:else}
    <!-- add notification button -->
    <div class="flex justify-end mb-4">
      <Button onclick={addNotification} class="cursor-pointer gap-2">
        <Plus />
        Add Notification Service
      </Button>
    </div>

    <!-- notifications list -->
    {#if notifications.length === 0}
      <div
        class="p-8 text-center border border-border rounded-lg bg-muted/20 text-muted-foreground"
      >
        <p>No notification services configured yet</p>
        <p class="text-sm mt-2">
          Click "Add Notification Service" to get started
        </p>
      </div>
    {:else}
      <div class="space-y-4">
        {#each notifications as notification, index}
          <div class="border border-border rounded-lg bg-card">
            <!-- collapsible header -->
            <div
              class="flex items-center justify-between p-4 cursor-pointer hover:bg-muted/20
                transition-colors gap-3"
              onclick={() => toggleExpanded(notification.id)}
              role="button"
              tabindex="0"
              onkeydown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  toggleExpanded(notification.id);
                }
              }}
            >
              <!-- left side: chevron + title -->
              <div class="flex items-center gap-2 flex-1 min-w-0">
                <div class="shrink-0">
                  {#if expandedStates[notification.id]}
                    <ChevronDown class="size-5 text-muted-foreground" />
                  {:else}
                    <ChevronRight class="size-5 text-muted-foreground" />
                  {/if}
                </div>

                <h3
                  class="text-base md:text-lg font-medium text-foreground truncate"
                >
                  {notification.name || `Notification Service ${index + 1}`}
                </h3>
              </div>

              <!-- right side: desktop only controls -->
              <div
                class="hidden md:flex items-center gap-3 shrink-0"
                onclick={(e) => e.stopPropagation()}
                role="none"
              >
                <label class="flex items-center gap-2 cursor-pointer">
                  <span class="text-sm text-foreground">Enabled</span>
                  <Switch
                    checked={notification.enabled}
                    onCheckedChange={(checked) =>
                      (notifications[index].enabled = checked)}
                  />
                </label>
                <Button
                  variant="destructive"
                  size="icon-sm"
                  onclick={() => deleteNotification(index)}
                  disabled={saving}
                  class="cursor-pointer"
                >
                  <Trash2 />
                </Button>
              </div>
            </div>

            <!-- collapsible content -->
            {#if expandedStates[notification.id]}
              <div
                class="px-4 md:px-6 pb-6 pt-4 space-y-4 border-t border-border"
              >
                <!-- mobile controls -->
                <div
                  class="flex md:hidden flex-col gap-3 pb-4 border-b border-border"
                >
                  <!-- edit title -->
                  <div>
                    <div
                      class="block text-xs font-medium text-muted-foreground mb-1.5"
                    >
                      Service Name
                    </div>
                    <div class="flex items-center gap-2">
                      {#if editingTitle === index}
                        <Input
                          type="text"
                          bind:value={editedTitleValue}
                          onkeydown={(e) => {
                            if (e.key === "Enter") saveTitle(index);
                            if (e.key === "Escape") cancelEditTitle();
                          }}
                          placeholder="Enter a name..."
                          class="input-hover-el px-3 py-2 bg-background border border-input rounded-lg 
                            text-foreground focus:outline-none focus:ring-2 focus:ring-ring text-sm flex-1"
                          maxlength={50}
                        />
                        <Button
                          size="icon"
                          variant="default"
                          class="cursor-pointer shrink-0"
                          onclick={() => saveTitle(index)}
                        >
                          <Check />
                        </Button>
                        <Button
                          size="icon"
                          variant="ghost"
                          class="cursor-pointer shrink-0"
                          onclick={() => cancelEditTitle()}
                        >
                          <X />
                        </Button>
                      {:else}
                        <Input
                          type="text"
                          value={notification.name || ""}
                          readonly
                          disabled
                          placeholder="Click pencil to edit..."
                          class="px-3 py-2 bg-background border border-input rounded-lg 
                            text-foreground focus:outline-none focus:ring-2 focus:ring-ring text-sm flex-1"
                        />
                        <Button
                          size="icon"
                          variant="outline"
                          class="cursor-pointer shrink-0"
                          onclick={() => startEditingTitle(index)}
                        >
                          <Pencil class="size-4" />
                        </Button>
                      {/if}
                    </div>
                  </div>

                  <!-- enable toggle and delete -->
                  <div class="flex items-center justify-between gap-3">
                    <label class="flex items-center gap-2 cursor-pointer">
                      <span class="text-sm text-foreground">Enabled</span>
                      <Switch
                        checked={notification.enabled}
                        onCheckedChange={(checked) =>
                          (notifications[index].enabled = checked)}
                      />
                    </label>
                    <Button
                      variant="destructive"
                      size="icon-sm"
                      onclick={() => deleteNotification(index)}
                      disabled={saving}
                      class="cursor-pointer"
                    >
                      <Trash2 />
                      Delete
                    </Button>
                  </div>
                </div>

                <!-- desktop edit title -->
                <div class="hidden md:block">
                  <div class="block text-sm font-medium text-foreground mb-2">
                    Service Name
                  </div>
                  <div class="flex items-center gap-2">
                    {#if editingTitle === index}
                      <Input
                        type="text"
                        bind:value={editedTitleValue}
                        onkeydown={(e) => {
                          if (e.key === "Enter") saveTitle(index);
                          if (e.key === "Escape") cancelEditTitle();
                        }}
                        placeholder="Enter a name..."
                        class="input-hover-el px-3 py-2 bg-background border border-input rounded-lg 
                          text-foreground focus:outline-none focus:ring-2 focus:ring-ring text-sm flex-1"
                        maxlength={50}
                      />
                      <Button
                        size="icon"
                        variant="default"
                        class="cursor-pointer"
                        onclick={() => saveTitle(index)}
                      >
                        <Check />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        class="cursor-pointer"
                        onclick={() => cancelEditTitle()}
                      >
                        <X />
                      </Button>
                    {:else}
                      <Input
                        type="text"
                        value={notification.name || ""}
                        readonly
                        disabled
                        placeholder="Click pencil to edit..."
                        class="px-3 py-2 bg-background border border-input rounded-lg 
                          text-foreground focus:outline-none focus:ring-2 focus:ring-ring text-sm flex-1"
                      />
                      <Button
                        size="icon"
                        variant="outline"
                        class="cursor-pointer"
                        onclick={() => startEditingTitle(index)}
                      >
                        <Pencil />
                      </Button>
                    {/if}
                  </div>
                  <p class="mt-1 text-xs text-muted-foreground">
                    Give this notification service a memorable name (optional)
                  </p>
                </div>

                <!-- apprise url input -->
                <div>
                  <label
                    for="url-{index}"
                    class="block text-sm font-medium text-foreground mb-2"
                  >
                    Apprise URL
                  </label>
                  <Textarea
                    id="url-{index}"
                    bind:value={notification.url}
                    placeholder="e.g., discord://webhook_id/webhook_token or tgram://bot_token/chat_id"
                    class="input-hover-el font-mono text-sm"
                    rows={3}
                  ></Textarea>
                  <p class="mt-1 text-xs text-muted-foreground">
                    Enter your Apprise-compatible notification URL. See <a
                      href="https://appriseit.com/getting-started/universal-syntax/"
                      target="_blank"
                      class="text-primary hover:underline"
                      >Apprise documentation</a
                    >
                    for supported
                    <a
                      href="https://appriseit.com/services/"
                      target="_blank"
                      class="text-primary hover:underline">services</a
                    >
                  </p>
                </div>

                <!-- notification types -->
                <div>
                  <h4 class="text-sm font-medium text-foreground mb-3">
                    Notification Types
                  </h4>
                  <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {#each notificationTypes as notifType}
                      {#if !notifType.adminOnly || isAdmin}
                        <label
                          class="flex items-start gap-3 p-3 border border-border rounded-lg cursor-pointer
                                 hover:bg-muted/50 transition-colors"
                        >
                          <Switch
                            checked={notification[
                              notificationTypeMap[notifType.type]
                            ]}
                            onCheckedChange={() =>
                              toggleNotificationType(index, notifType.type)}
                            class="mt-0.5"
                          />
                          <div class="flex-1">
                            <div class="flex items-center gap-2">
                              <span class="text-sm font-medium text-foreground">
                                {notifType.label}
                              </span>
                              {#if notifType.adminOnly}
                                <Badge variant="secondary" class="text-xs">
                                  Admin Only
                                </Badge>
                              {/if}
                            </div>
                            <p class="text-xs text-muted-foreground mt-0.5">
                              {notifType.description}
                            </p>
                          </div>
                        </label>
                      {/if}
                    {/each}
                  </div>
                </div>

                <!-- controls -->
                <div class="flex justify-end gap-2 pt-2">
                  <!-- test -->
                  <Button
                    onclick={() => testNotification(index)}
                    disabled={testingIndex !== null || saving}
                    class="cursor-pointer gap-2"
                  >
                    {#if testingIndex === index}
                      <Spinner class="size-4" />
                    {:else}
                      <TestTube class="size-4" />
                    {/if}
                    Test
                  </Button>
                  <!-- save -->
                  <Button
                    onclick={() => saveNotification(index)}
                    disabled={testingIndex !== null || saving}
                    class="cursor-pointer gap-2"
                  >
                    {#if saving}
                      <Spinner class="size-4" />
                    {:else}
                      <Save class="size-4" />
                    {/if}
                    Save
                  </Button>
                </div>
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  {/if}
</div>
