<script lang="ts">
  import { onMount } from "svelte";
  import type { Component } from "svelte";
  import { auth } from "$lib/stores/auth";
  import { get_api, post_api, delete_api } from "$lib/api";
  import ErrorBox from "$lib/components/error-box.svelte";
  import {
    type MediaIdentityItem,
    type MediaIdentityListResponse,
    PageAccess,
    Permission,
    type UserProfile as User,
    UserRole,
  } from "$lib/types/shared";
  import {
    DEFAULT_NEW_USER_ALLOWED_PAGES,
    PAGE_ACCESS_OPTIONS,
  } from "$lib/page-access";
  import { Button } from "$lib/components/ui/button/index.js";
  import { toast } from "svelte-sonner";
  import { formatDate } from "$lib/utils/date";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { Checkbox } from "$lib/components/ui/checkbox/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import * as Avatar from "$lib/components/ui/avatar/index.js";
  import UserPlus from "@lucide/svelte/icons/user-plus";
  import UserPen from "@lucide/svelte/icons/user-pen";
  import UserX from "@lucide/svelte/icons/user-x";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import Link2 from "@lucide/svelte/icons/link-2";
  import X from "@lucide/svelte/icons/x";

  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  let users: User[] = $state([]);
  let mediaIdentities: MediaIdentityItem[] = $state([]);
  let mediaIdentitiesLoading = $state(false);
  let mediaIdentitiesError = $state("");
  let identitySearch = $state("");
  let identityBusyIds = $state<number[]>([]);
  let linkTargets = $state<Record<number, string>>({});
  const filteredMediaIdentities = $derived.by(() => {
    const query = identitySearch.trim().toLowerCase();
    if (!query) return mediaIdentities;
    return mediaIdentities.filter((item) => {
      return (
        item.source_service_name.toLowerCase().includes(query) ||
        item.source_service.toLowerCase().includes(query) ||
        item.username.toLowerCase().includes(query) ||
        item.source_user_id.toLowerCase().includes(query) ||
        (item.email || "").toLowerCase().includes(query) ||
        (item.user_username || "").toLowerCase().includes(query) ||
        (item.user_display_name || "").toLowerCase().includes(query)
      );
    });
  });
  const isAdmin = $derived($auth.user?.role === "admin");
  const canManageUsers = $derived(
    isAdmin || ($auth.user?.permissions ?? []).includes(Permission.ManageUsers),
  );

  let loading: boolean = $state(true);
  let profileError: string = $state("");
  let showCreateModal: boolean = $state(false);
  let showEditModal: boolean = $state(false);
  let editingUser: User | null = $state(null);

  let userToDelete: User | null = $state(null);
  let showDeleteDialog: boolean = $state(false);
  const defaultCreateAllowedPages = DEFAULT_NEW_USER_ALLOWED_PAGES;

  // create user form
  let newUser = $state({
    username: "",
    password: "",
    email: "",
    display_name: "",
    role: "user",
    permissions: [Permission.Request],
    use_default_page_access: true,
    allowed_pages: [...defaultCreateAllowedPages] as PageAccess[] | null,
  });

  // edit user form
  let editUser = $state({
    display_name: "",
    email: "",
    role: "user",
    permissions: [] as Permission[],
    allowed_pages: null as PageAccess[] | null,
    password: "", // optional - only if changing password
  });

  const permissionOptions = [
    {
      value: Permission.ManageUsers,
      label: "Manage users",
      description:
        "Create, edit, and delete users (cannot manage admins unless Admin)",
    },
    {
      value: Permission.ManageRequests,
      label: "Manage requests",
      description: "View all requests and approve/deny requests",
    },
    {
      value: Permission.Request,
      label: "Request",
      description: "Submit and manage own exception requests",
    },
    {
      value: Permission.AutoApprove,
      label: "Auto approve",
      description: "Automatically approve your own requests",
    },
    {
      value: Permission.ManageProtection,
      label: "Manage protection",
      description: "Manage protected media entries in the protection list",
    },
  ];

  // helper to check if a permission can be edited based on current user's role
  const canEditPermission = (_permission: Permission): boolean => true;

  const selectedPageLabels = (pages: PageAccess[] | null): string => {
    if (pages === null) return "Unrestricted";
    if (pages.length === 0) return "No pages";
    const labels = PAGE_ACCESS_OPTIONS.filter((option) =>
      pages.includes(option.value),
    ).map((option) => option.label);
    return labels.join(", ");
  };

  const isIdentityBusy = (identityId: number) =>
    identityBusyIds.includes(identityId);

  const seedLinkTargets = () => {
    const next: Record<number, string> = { ...linkTargets };
    for (const identity of mediaIdentities) {
      if (identity.user_id) {
        next[identity.id] = String(identity.user_id);
        continue;
      }
      if (next[identity.id]) continue;

      const usernameMatch = users.find(
        (user) =>
          user.username.toLowerCase() ===
          identity.username_normalized.toLowerCase(),
      );
      if (usernameMatch) {
        next[identity.id] = String(usernameMatch.id);
        continue;
      }

      if (identity.email) {
        const emailMatch = users.find(
          (user) =>
            user.email &&
            user.email.toLowerCase() === identity.email?.toLowerCase(),
        );
        if (emailMatch) {
          next[identity.id] = String(emailMatch.id);
        }
      }
    }
    linkTargets = next;
  };

  // toggle permission in create user form
  const toggleCreatePermission = (permission: Permission, checked: boolean) => {
    if (checked) {
      newUser.permissions = [...newUser.permissions, permission];
    } else {
      newUser.permissions = newUser.permissions.filter((p) => p !== permission);
    }
  };

  // toggle permission in edit user form
  const toggleEditPermission = (permission: Permission, checked: boolean) => {
    if (checked) {
      editUser.permissions = [...editUser.permissions, permission];
    } else {
      editUser.permissions = editUser.permissions.filter(
        (p) => p !== permission,
      );
    }
  };

  const toggleCreatePageAccess = (page: PageAccess, checked: boolean) => {
    if (newUser.allowed_pages === null) return;
    if (checked) {
      newUser.allowed_pages = [...new Set([...newUser.allowed_pages, page])];
      return;
    }
    newUser.allowed_pages = newUser.allowed_pages.filter(
      (item) => item !== page,
    );
  };

  const toggleEditPageAccess = (page: PageAccess, checked: boolean) => {
    if (editUser.allowed_pages === null) return;
    if (checked) {
      editUser.allowed_pages = [...new Set([...editUser.allowed_pages, page])];
      return;
    }
    editUser.allowed_pages = editUser.allowed_pages.filter(
      (item) => item !== page,
    );
  };

  const setCreateUnrestrictedPageAccess = (checked: boolean) => {
    newUser.allowed_pages = checked ? null : [...defaultCreateAllowedPages];
  };

  const setEditUnrestrictedPageAccess = (checked: boolean) => {
    editUser.allowed_pages = checked ? null : [...defaultCreateAllowedPages];
  };

  // load users from API
  const loadUsers = async () => {
    try {
      profileError = "";
      users = await get_api<User[]>("/api/account/users");
      seedLinkTargets();
    } catch (err: any) {
      profileError = err.message;
    } finally {
      loading = false;
    }
  };

  const loadMediaIdentities = async () => {
    try {
      mediaIdentitiesLoading = true;
      mediaIdentitiesError = "";
      const response = await get_api<MediaIdentityListResponse>(
        "/api/account/users/media-identities",
      );
      mediaIdentities = response.items;
      seedLinkTargets();
    } catch (err: any) {
      mediaIdentitiesError = err.message;
    } finally {
      mediaIdentitiesLoading = false;
    }
  };

  const linkIdentity = async (identity: MediaIdentityItem) => {
    const targetValue = linkTargets[identity.id];
    const targetUserId = Number(targetValue);
    if (!targetUserId || Number.isNaN(targetUserId)) {
      toast.warning("Select a local user to link this media identity.");
      return;
    }

    try {
      identityBusyIds = [...identityBusyIds, identity.id];
      await post_api(
        `/api/account/users/media-identities/${identity.id}/link`,
        {
          target_user_id: targetUserId,
        },
      );
      await loadMediaIdentities();
      toast.success("Media identity linked.");
    } catch (err: any) {
      toast.warning(err.message);
    } finally {
      identityBusyIds = identityBusyIds.filter((id) => id !== identity.id);
    }
  };

  const unlinkIdentity = async (identity: MediaIdentityItem) => {
    try {
      identityBusyIds = [...identityBusyIds, identity.id];
      await post_api(
        `/api/account/users/media-identities/${identity.id}/unlink`,
      );
      await loadMediaIdentities();
      toast.success("Media identity unlinked.");
    } catch (err: any) {
      toast.warning(err.message);
    } finally {
      identityBusyIds = identityBusyIds.filter((id) => id !== identity.id);
    }
  };

  // create new user
  const createUser = async () => {
    try {
      await post_api("/api/account/users", newUser);
      showCreateModal = false;
      resetForm();
      await Promise.all([loadUsers(), loadMediaIdentities()]);
    } catch (err: any) {
      toast.warning(err.message);
    }
  };

  // delete user
  const deleteUser = async (userId: number, username: string) => {
    try {
      profileError = "";
      await delete_api(`/api/account/users/${userId}`);
      await Promise.all([loadUsers(), loadMediaIdentities()]);
      toast.success(`User "${username}" has been deleted.`);
    } catch (err: any) {
      profileError = err.message;
    }
  };

  // open delete confirmation dialog
  const openDeleteDialog = (user: User) => {
    userToDelete = user;
    showDeleteDialog = true;
  };

  // close delete confirmation dialog
  const closeDeleteDialog = () => {
    showDeleteDialog = false;
    // small delay to allow dialog close animation before removing value from state
    setTimeout(() => {
      userToDelete = null;
    }, 200);
  };

  // confirm delete user
  const confirmDelete = async () => {
    if (userToDelete) {
      await deleteUser(userToDelete.id, userToDelete.username);
      closeDeleteDialog();
    }
  };

  // open edit user modal
  const openEditModal = (user: User) => {
    editingUser = user;
    editUser = {
      display_name: user.display_name || "",
      email: user.email || "",
      role: user.role,
      permissions: [...(user.permissions || [])],
      allowed_pages:
        user.allowed_pages === null ? null : [...user.allowed_pages],
      password: "",
    };
    showEditModal = true;
  };

  // close edit user modal
  const closeEditModal = () => {
    showEditModal = false;
    editingUser = null;
    editUser = {
      display_name: "",
      email: "",
      role: "user",
      permissions: [],
      allowed_pages: null,
      password: "",
    };
  };

  // update user
  const updateUser = async () => {
    if (!editingUser) return;

    try {
      const updateData: any = {
        display_name: editUser.display_name.trim(),
        email: editUser.email.trim(),
        role: editUser.role,
        permissions: editUser.permissions,
        allowed_pages:
          editUser.role === UserRole.Admin ? null : editUser.allowed_pages,
      };

      // only include password if it's provided
      let pwTrimmed = editUser.password.trim();
      if (pwTrimmed) {
        updateData.password = pwTrimmed;
      }

      // send update request
      const preEditedUser = { ...editingUser }; // keep a copy since closeEditModal will reset editingUser
      await post_api(`/api/account/users/${editingUser.id}`, updateData);
      closeEditModal();
      await Promise.all([loadUsers(), loadMediaIdentities()]);
      toast.success(`User "${preEditedUser.username}" has been updated.`);
    } catch (err: any) {
      toast.warning(err.message);
    }
  };

  // close create user modal and reset form
  const closeModal = () => {
    showCreateModal = false;
    resetForm();
  };

  // reset create user form
  const resetForm = () => {
    newUser = {
      username: "",
      password: "",
      email: "",
      display_name: "",
      role: "user",
      permissions: [Permission.Request],
      use_default_page_access: true,
      allowed_pages: [...defaultCreateAllowedPages],
    };
  };

  onMount(() => {
    loadUsers();
    loadMediaIdentities();
  });
</script>

<!-- header -->
<div class="flex items-center justify-between mb-4">
  <div>
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {#if svgIcon}
        {@const Icon = svgIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      <span class="align-middle">Users</span>
    </h2>
    <p class="text-sm text-muted-foreground mt-1">
      Create and manage user accounts
    </p>
  </div>
  {#if !profileError && canManageUsers}
    <Button
      type="button"
      class="hover cursor-pointer"
      onclick={() => (showCreateModal = true)}><UserPlus /> Add User</Button
    >
  {/if}
</div>

<ErrorBox error={profileError} />

<!-- users table -->
<div class="bg-card rounded-lg border border-border overflow-x-auto">
  {#if loading}
    <div class="p-8 text-center text-muted-foreground">
      <div
        class="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-primary border-r-transparent"
      ></div>
      <p class="mt-4">Loading users...</p>
    </div>
  {:else if users.length === 0}
    <div class="p-8 text-center text-muted-foreground">
      No users found. Create your first user to get started.
    </div>
  {:else}
    <table class="w-full">
      <thead class="bg-muted/50">
        <tr>
          <th
            class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
            >User</th
          >
          <th
            class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
            >Role</th
          >
          <th
            class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
            >Page Access</th
          >
          <th
            class="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
            >Joined</th
          >
          <th
            class="px-6 py-3 text-right text-xs font-medium text-muted-foreground uppercase tracking-wider"
            >Actions</th
          >
        </tr>
      </thead>
      <tbody class="divide-y divide-border">
        {#each users as user (user.id)}
          <tr class="hover:bg-muted/30 transition-colors">
            <td class="px-6 py-4 whitespace-nowrap">
              <div class="flex items-center gap-3">
                <div class="relative">
                  {#if user.avatar_url}
                    <Avatar.Root class="w-16 h-16 border-2 border-primary">
                      <Avatar.Image src={user.avatar_url} alt={user.username} />
                      <Avatar.Fallback
                        >{user.username
                          .charAt(0)
                          .toUpperCase()}</Avatar.Fallback
                      >
                    </Avatar.Root>
                  {:else}
                    <Avatar.Root
                      class="w-16 h-16 text-2xl text-primary-foreground font-bold"
                    >
                      <Avatar.Fallback class="bg-primary">
                        {user.username.charAt(0).toUpperCase()}
                      </Avatar.Fallback>
                    </Avatar.Root>
                  {/if}
                </div>

                <div>
                  <div
                    class="flex flex-col text-sm font-medium text-foreground"
                  >
                    <span>{user.username}</span>
                    <span class="text-xs text-muted-foreground italic"
                      >{user.display_name}</span
                    >
                  </div>
                  {#if user.email}
                    <div class="text-xs text-muted-foreground">
                      {user.email}
                    </div>
                  {/if}
                </div>
              </div>
            </td>
            <td class="px-6 py-4 whitespace-nowrap">
              <span class="text-sm text-foreground capitalize">{user.role}</span
              >
            </td>
            <td class="px-6 py-4 max-w-80">
              <span class="text-sm text-muted-foreground">
                {user.role === UserRole.Admin
                  ? "Admin override"
                  : selectedPageLabels(user.allowed_pages)}
              </span>
            </td>
            <td
              class="px-6 py-4 whitespace-nowrap text-sm text-muted-foreground"
            >
              {formatDate(user.created_at)}
            </td>
            <td
              class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium"
            >
              <div class="flex items-center justify-end gap-2">
                <!-- edit -->
                {#if canManageUsers}
                  <Button
                    type="button"
                    size="icon"
                    class="rounded-full cursor-pointer"
                    onclick={() => openEditModal(user)}><UserPen /></Button
                  >
                {/if}
                <!-- delete -->
                {#if canManageUsers}
                  <Button
                    type="button"
                    size="icon"
                    class="rounded-full hover:bg-destructive-secondary bg-destructive 
                    text-destructive-foreground disabled:bg-muted disabled:text-muted-foreground 
                    disabled:cursor-not-allowed cursor-pointer"
                    onclick={() => openDeleteDialog(user)}
                    disabled={user.id === $auth.user?.id}><UserX /></Button
                  >
                {/if}
              </div>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<!-- media identity linking -->
<div class="mt-6 bg-card rounded-lg border border-border overflow-x-auto">
  <div class="p-4 border-b border-border flex flex-wrap items-end gap-3">
    <div class="grow">
      <h3 class="text-base font-semibold text-foreground">
        Media Identity Links
      </h3>
      <p class="text-sm text-muted-foreground">
        Review media server users and link them to local accounts.
      </p>
    </div>
    <div class="flex items-center gap-2">
      <Input
        type="text"
        placeholder="Search identities"
        bind:value={identitySearch}
        class="w-full sm:w-72 input-hover-el"
      />
      <Button
        type="button"
        size="sm"
        class="cursor-pointer"
        disabled={mediaIdentitiesLoading}
        onclick={() => loadMediaIdentities()}
      >
        <RefreshCw class="size-4" />
        Refresh
      </Button>
    </div>
  </div>

  {#if mediaIdentitiesLoading}
    <div class="p-6 text-sm text-muted-foreground">
      Loading media identities...
    </div>
  {:else if mediaIdentitiesError}
    <div class="p-6 text-sm text-destructive">{mediaIdentitiesError}</div>
  {:else if filteredMediaIdentities.length === 0}
    <div class="p-6 text-sm text-muted-foreground">
      No media identities found.
    </div>
  {:else}
    <table class="w-full">
      <thead class="bg-muted/40">
        <tr>
          <th
            class="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
          >
            Source
          </th>
          <th
            class="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
          >
            Media User
          </th>
          <th
            class="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
          >
            Linked User
          </th>
          <th
            class="px-4 py-3 text-right text-xs font-medium text-muted-foreground uppercase tracking-wider"
          >
            Actions
          </th>
        </tr>
      </thead>
      <tbody class="divide-y divide-border">
        {#each filteredMediaIdentities as identity (identity.id)}
          <tr class="hover:bg-muted/20 transition-colors">
            <td class="px-4 py-3 align-top min-w-44">
              <div class="text-sm text-foreground">
                {identity.source_service_name}
              </div>
            </td>
            <td class="px-4 py-3 align-top min-w-72">
              <div class="text-sm text-foreground">{identity.username}</div>
              {#if identity.email}
                <div class="text-xs text-muted-foreground">
                  {identity.email}
                </div>
              {/if}
              <div class="text-xs text-muted-foreground break-all font-mono">
                ID: {identity.source_user_id}
              </div>
            </td>
            <td class="px-4 py-3 align-top min-w-72">
              {#if identity.user_id}
                <div class="text-sm text-foreground">
                  {identity.user_display_name || identity.user_username}
                </div>
                <div class="text-xs text-muted-foreground">
                  @{identity.user_username}
                </div>
              {:else}
                <div class="text-sm text-amber-400">Unlinked</div>
              {/if}

              <div class="mt-2 text-foreground">
                <Select.Root
                  type="single"
                  bind:value={linkTargets[identity.id]}
                >
                  <Select.Trigger
                    class="w-full max-w-80 focus:ring-2 focus:ring-ring"
                  >
                    {#if linkTargets[identity.id]}
                      {@const selectedUser = users.find(
                        (user) => String(user.id) === linkTargets[identity.id],
                      )}
                      {selectedUser
                        ? `${selectedUser.display_name || selectedUser.username} (@${selectedUser.username})`
                        : "Select local user"}
                    {:else}
                      Select local user
                    {/if}
                  </Select.Trigger>
                  <Select.Content>
                    {#each users as user (user.id)}
                      <Select.Item
                        value={String(user.id)}
                        label={`${user.display_name || user.username} (@${user.username})`}
                      >
                        {user.display_name || user.username} (@{user.username})
                      </Select.Item>
                    {/each}
                  </Select.Content>
                </Select.Root>
              </div>
            </td>
            <td class="px-4 py-3 align-top text-right min-w-52">
              <div class="flex items-center justify-end gap-2">
                {#if identity.user_id}
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    class="cursor-pointer"
                    disabled={isIdentityBusy(identity.id)}
                    onclick={() => unlinkIdentity(identity)}
                  >
                    <X class="size-4" />
                    Unlink
                  </Button>
                {:else}
                  <Button
                    type="button"
                    size="sm"
                    class="cursor-pointer"
                    disabled={isIdentityBusy(identity.id) ||
                      !linkTargets[identity.id]}
                    onclick={() => linkIdentity(identity)}
                  >
                    <Link2 class="size-4" />
                    Link
                  </Button>
                {/if}
              </div>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<!-- create user modal -->
{#if showCreateModal}
  <Dialog.Root
    open={showCreateModal}
    onOpenChange={(v) => (showCreateModal = v)}
  >
    <Dialog.Content
      class="max-h-[90vh] bg-card rounded-lg border border-border max-w-md w-full p-6 overflow-y-auto
        text-foreground"
    >
      <Dialog.Header>
        <Dialog.Title class="text-xl font-semibold text-foreground mb-4"
          >Create Local User</Dialog.Title
        >
      </Dialog.Header>
      <form
        onsubmit={(e) => {
          e.preventDefault();
          createUser();
        }}
        class="space-y-4"
        autocomplete="off"
      >
        <div class="space-y-2">
          <Label for="username">Username</Label>
          <Input
            id="username"
            type="text"
            class="input-hover-el"
            bind:value={newUser.username}
            required
            placeholder="Enter username"
            minlength={5}
            maxlength={32}
          />
        </div>
        <div class="space-y-2">
          <Label for="password">Password</Label>
          <Input
            id="password"
            type="password"
            class="input-hover-el"
            bind:value={newUser.password}
            required
            placeholder="Enter password"
            minlength={3}
            maxlength={64}
          />
        </div>
        <div class="space-y-2">
          <Label for="display_name">Display Name (Optional)</Label>
          <Input
            id="display_name"
            type="text"
            class="input-hover-el"
            bind:value={newUser.display_name}
            placeholder="Enter display name"
            minlength={3}
            maxlength={32}
          />
        </div>
        <div class="space-y-2">
          <Label for="email">Email (Optional)</Label>
          <Input
            id="email"
            type="email"
            class="input-hover-el"
            bind:value={newUser.email}
            placeholder="Enter email"
            minlength={5}
            maxlength={120}
          />
        </div>
        <div class="space-y-2">
          <Label for="role">Role</Label>
          <Select.Root type="single" bind:value={newUser.role}>
            <Select.Trigger class="w-full focus:ring-2 focus:ring-ring">
              {newUser.role === "admin" ? "Admin" : "User"}
            </Select.Trigger>
            <Select.Content>
              <Select.Item value="user" label="User">User</Select.Item>
              {#if isAdmin}
                <Select.Item value="admin" label="Admin">Admin</Select.Item>
              {/if}
            </Select.Content>
          </Select.Root>
        </div>
        <div class="space-y-2">
          <Label for="new_permissions_container">Permissions</Label>
          {#if newUser.role === UserRole.Admin}
            <span class="mt-0 text-xs text-muted-foreground"
              >Permissions have no effect on Admins</span
            >
          {/if}
          <div
            id="new_permissions_container"
            class="space-y-2 rounded-lg border border-border p-3"
          >
            {#each permissionOptions as option}
              <Label
                class="flex items-start gap-2 text-sm text-foreground {canEditPermission(
                  option.value,
                )
                  ? 'cursor-pointer'
                  : 'opacity-50 cursor-not-allowed'}"
              >
                <Checkbox
                  checked={newUser.permissions.includes(option.value)}
                  disabled={!canEditPermission(option.value)}
                  onCheckedChange={(e) =>
                    toggleCreatePermission(option.value, e)}
                />
                <span>
                  <span class="font-medium">{option.label}</span>
                  <span class="block text-xs text-muted-foreground"
                    >{option.description}</span
                  >
                </span>
              </Label>
            {/each}
          </div>
        </div>
        <div class="space-y-2">
          <Label for="new_page_access_container">Page Access</Label>
          {#if newUser.role === UserRole.Admin}
            <span class="mt-0 text-xs text-muted-foreground">
              Admins can access all pages.
            </span>
          {:else}
            <div
              id="new_page_access_container"
              class="space-y-3 rounded-lg border border-border p-3"
            >
              <Label
                class="flex items-start gap-2 text-sm text-foreground cursor-pointer"
              >
                <Checkbox
                  checked={newUser.use_default_page_access}
                  onCheckedChange={(checked) =>
                    (newUser.use_default_page_access = checked === true)}
                />
                <span>
                  <span class="font-medium">Use configured default</span>
                  <span class="block text-xs text-muted-foreground">
                    Applies the General settings default when this user is
                    created.
                  </span>
                </span>
              </Label>
              {#if !newUser.use_default_page_access}
                <Label
                  class="flex items-start gap-2 text-sm text-foreground cursor-pointer"
                >
                  <Checkbox
                    checked={newUser.allowed_pages === null}
                    onCheckedChange={(checked) =>
                      setCreateUnrestrictedPageAccess(checked === true)}
                  />
                  <span>
                    <span class="font-medium">Unrestricted page access</span>
                    <span class="block text-xs text-muted-foreground">
                      Allow all current and future user-visible pages.
                    </span>
                  </span>
                </Label>
                {#if newUser.allowed_pages !== null}
                  <div class="grid gap-2 sm:grid-cols-2">
                    {#each PAGE_ACCESS_OPTIONS as option}
                      <Label
                        class="flex items-start gap-2 rounded-md border border-border p-2 text-sm text-foreground cursor-pointer"
                      >
                        <Checkbox
                          checked={newUser.allowed_pages.includes(option.value)}
                          onCheckedChange={(checked) =>
                            toggleCreatePageAccess(
                              option.value,
                              checked === true,
                            )}
                        />
                        <span>
                          <span class="font-medium">{option.label}</span>
                          <span class="block text-xs text-muted-foreground">
                            {option.description}
                          </span>
                        </span>
                      </Label>
                    {/each}
                  </div>
                  {#if newUser.allowed_pages.length === 0}
                    <p class="text-xs text-destructive">
                      Select at least one page.
                    </p>
                  {/if}
                {/if}
              {/if}
            </div>
          {/if}
        </div>
        <div class="flex justify-between gap-3 pt-4">
          <Button
            type="button"
            class="flex-1 hover cursor-pointer"
            onclick={closeModal}
            variant="secondary">Cancel</Button
          >
          <Button type="submit" class="flex-1 hover cursor-pointer"
            >Save Changes</Button
          >
        </div>
      </form>
    </Dialog.Content>
  </Dialog.Root>
{/if}

<!-- edit user modal -->
{#if showEditModal && editingUser}
  <Dialog.Root open={showEditModal} onOpenChange={(v) => (showEditModal = v)}>
    <Dialog.Content
      class="max-h-[90vh] bg-card rounded-lg border border-border max-w-md w-full p-6 overflow-y-auto
        text-foreground"
    >
      <Dialog.Header>
        <Dialog.Title class="text-xl font-semibold text-foreground mb-4"
          >Edit User: {editingUser.username}</Dialog.Title
        >
      </Dialog.Header>
      <form
        onsubmit={(e) => {
          (e.preventDefault(), updateUser());
        }}
        class="space-y-4"
        autocomplete="off"
      >
        <div class="space-y-2">
          <Label for="edit_display_name">Display Name</Label>
          <Input
            id="edit_display_name"
            type="text"
            bind:value={editUser.display_name}
            placeholder="Enter display name"
            class="input-hover-el"
            minlength={3}
            maxlength={32}
          />
        </div>
        <div class="space-y-2">
          <Label for="edit_email">Email</Label>
          <Input
            id="edit_email"
            type="email"
            bind:value={editUser.email}
            placeholder="Enter email"
            class="input-hover-el"
            minlength={5}
            maxlength={120}
          />
        </div>
        <div class="space-y-2">
          <Label for="edit_role">Role</Label>
          <Select.Root type="single" bind:value={editUser.role}>
            <Select.Trigger class="w-full focus:ring-2 focus:ring-ring">
              {editUser.role === "admin" ? "Admin" : "User"}
            </Select.Trigger>
            <Select.Content>
              <Select.Item value="user" label="User">User</Select.Item>
              {#if isAdmin}
                <Select.Item value="admin" label="Admin">Admin</Select.Item>
              {/if}
            </Select.Content>
          </Select.Root>
        </div>
        <div class="space-y-2">
          <Label for="edit_permissions_container">Permissions</Label>
          {#if editUser.role === UserRole.Admin}
            <span class="mt-0 text-xs text-muted-foreground"
              >Permissions have no effect on Admins</span
            >
          {/if}
          <div
            id="edit_permissions_container"
            class="space-y-2 rounded-lg border border-border p-3"
          >
            {#each permissionOptions as option}
              <Label
                class="flex items-start gap-2 text-sm text-foreground {canEditPermission(
                  option.value,
                )
                  ? 'cursor-pointer'
                  : 'opacity-50 cursor-not-allowed'}"
              >
                <Checkbox
                  checked={editUser.permissions.includes(option.value)}
                  disabled={!canEditPermission(option.value)}
                  onCheckedChange={(e) => toggleEditPermission(option.value, e)}
                />
                <span>
                  <span class="font-medium">{option.label}</span>
                  <span class="block text-xs text-muted-foreground"
                    >{option.description}</span
                  >
                </span>
              </Label>
            {/each}
          </div>
        </div>
        <div class="space-y-2">
          <Label for="edit_page_access_container">Page Access</Label>
          {#if editUser.role === UserRole.Admin}
            <span class="mt-0 text-xs text-muted-foreground">
              Admins can access all pages.
            </span>
          {:else}
            <div
              id="edit_page_access_container"
              class="space-y-3 rounded-lg border border-border p-3"
            >
              <Label
                class="flex items-start gap-2 text-sm text-foreground cursor-pointer"
              >
                <Checkbox
                  checked={editUser.allowed_pages === null}
                  onCheckedChange={(checked) =>
                    setEditUnrestrictedPageAccess(checked === true)}
                />
                <span>
                  <span class="font-medium">Unrestricted page access</span>
                  <span class="block text-xs text-muted-foreground">
                    Allow all current and future user-visible pages.
                  </span>
                </span>
              </Label>
              {#if editUser.allowed_pages !== null}
                <div class="grid gap-2 sm:grid-cols-2">
                  {#each PAGE_ACCESS_OPTIONS as option}
                    <Label
                      class="flex items-start gap-2 rounded-md border border-border p-2 text-sm text-foreground cursor-pointer"
                    >
                      <Checkbox
                        checked={editUser.allowed_pages.includes(option.value)}
                        onCheckedChange={(checked) =>
                          toggleEditPageAccess(option.value, checked === true)}
                      />
                      <span>
                        <span class="font-medium">{option.label}</span>
                        <span class="block text-xs text-muted-foreground">
                          {option.description}
                        </span>
                      </span>
                    </Label>
                  {/each}
                </div>
                {#if editUser.allowed_pages.length === 0}
                  <p class="text-xs text-destructive">
                    Select at least one page.
                  </p>
                {/if}
              {/if}
            </div>
          {/if}
        </div>
        <div class="space-y-2">
          <Label for="edit_password">New Password (Optional)</Label>
          <Input
            id="edit_password"
            type="password"
            bind:value={editUser.password}
            placeholder="Leave blank to keep current password"
            class="input-hover-el"
            minlength={3}
            maxlength={64}
          />
          <p class="mt-1 text-xs text-muted-foreground">
            Only fill this if you want to change the user's password
          </p>
        </div>
        <div class="flex justify-between gap-3 pt-4">
          <Button
            type="button"
            class="flex-1 hover cursor-pointer"
            onclick={closeEditModal}
            variant="secondary">Cancel</Button
          >
          <Button type="submit" class="flex-1 hover cursor-pointer"
            >Save Changes</Button
          >
        </div>
      </form>
    </Dialog.Content>
  </Dialog.Root>
{/if}

<!-- confirm delete alert dialog -->
<AlertDialog.Root
  open={showDeleteDialog}
  onOpenChange={(v) => (showDeleteDialog = v)}
>
  <AlertDialog.Content
    class="bg-card border border-border rounded-lg p-6 max-w-md w-full text-foreground"
  >
    <AlertDialog.Header>
      <AlertDialog.Title class="text-xl font-semibold text-foreground mb-2"
        >Delete User</AlertDialog.Title
      >
      <AlertDialog.Description class="text-muted-foreground">
        Are you sure you want to delete user "{userToDelete?.username}"? This
        action cannot be undone.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer class="flex justify-end gap-3 pt-4">
      <AlertDialog.Cancel
        class="cursor-pointer hover text-foreground bg-secondary"
        onclick={closeDeleteDialog}
      >
        Cancel
      </AlertDialog.Cancel>
      <AlertDialog.Action class="cursor-pointer hover" onclick={confirmDelete}>
        Delete
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
