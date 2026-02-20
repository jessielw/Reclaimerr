<script lang="ts">
  import { onMount } from "svelte";
  import type { Component } from "svelte";
  import { auth } from "$lib/stores/auth";
  import { get_api, post_api, delete_api } from "$lib/api";
  import ErrorBox from "$lib/components/ErrorBox.svelte";
  import {
    Permission,
    type UserProfile as User,
    UserRole,
  } from "$lib/types/shared";
  import { Button } from "$lib/components/ui/button/index.js";
  import { toast } from "svelte-sonner";
  import { formatDate } from "$lib/utils/date";
  import * as AlertDialog from "$lib/components/ui/alert-dialog/index.js";
  import UserPlus from "@lucide/svelte/icons/user-plus";
  import UserPen from "@lucide/svelte/icons/user-pen";
  import UserX from "@lucide/svelte/icons/user-x";

  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  let users: User[] = $state([]);
  const isAdmin = $derived($auth.user?.role === "admin");
  const canManageUsers = $derived(
    isAdmin || ($auth.user?.permissions ?? []).includes(Permission.Moderator),
  );

  let loading: boolean = $state(true);
  let profileError: string = $state("");
  let showCreateModal: boolean = $state(false);
  let showEditModal: boolean = $state(false);
  let editingUser: User | null = $state(null);

  let userToDelete: User | null = $state(null);
  let showDeleteDialog: boolean = $state(false);

  // create user form
  let newUser = $state({
    username: "",
    password: "",
    email: "",
    display_name: "",
    role: "user",
    permissions: [Permission.Request],
  });

  // edit user form
  let editUser = $state({
    display_name: "",
    email: "",
    role: "user",
    permissions: [] as Permission[],
    password: "", // optional - only if changing password
  });

  const permissionOptions = [
    {
      value: Permission.Moderator,
      label: "Moderator",
      description: "Manage users (cannot grant or modify Admin unless Admin)",
      adminOnly: true,
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
      value: Permission.ManageBlocklist,
      label: "Manage blocklist",
      description: "Reserved for explicit blocklist management",
    },
  ];
  
  // helper to check if a permission can be edited based on current user's role
  const canEditPermission = (permission: Permission): boolean => {
    if (isAdmin) return true;
    return permission !== Permission.Moderator;
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

  // load users from API
  const loadUsers = async () => {
    try {
      profileError = "";
      users = await get_api<User[]>("/api/account/users");
    } catch (err: any) {
      profileError = err.message;
    } finally {
      loading = false;
    }
  };

  // create new user
  const createUser = async () => {
    try {
      await post_api("/api/account/users", newUser);
      showCreateModal = false;
      resetForm();
      await loadUsers();
    } catch (err: any) {
      toast.warning(err.message);
    }
  };

  // delete user
  const deleteUser = async (userId: number, username: string) => {
    try {
      profileError = "";
      await delete_api(`/api/account/users/${userId}`);
      await loadUsers();
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
      };

      // only include password if it's provided
      let pwTrimmed = editUser.password.trim();
      if (pwTrimmed) {
        updateData.password = pwTrimmed;
      }

      // send update request
      await post_api(`/api/account/users/${editingUser.id}`, updateData);
      closeEditModal();
      await loadUsers();
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
    };
  };

  onMount(() => {
    loadUsers();
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
                    <div>
                      <img
                        src={user.avatar_url}
                        alt="Avatar"
                        class="w-16 h-16 max-w-16 rounded-full object-cover border-4 border-primary"
                      />
                    </div>
                  {:else}
                    <div
                      class="w-16 h-16 rounded-full bg-primary text-primary-foreground flex items-center
                      justify-center text-2xl font-bold border-4 border-primary"
                    >
                      {user.username.charAt(0).toUpperCase()}
                    </div>
                  {/if}
                </div>

                <div>
                  <div class="text-sm font-medium text-foreground">
                    {user.display_name || user.username}
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

<!-- create User Modal -->
{#if showCreateModal}
  <div
    class="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
  >
    <div
      class="max-h-[90vh] bg-card rounded-lg border border-border max-w-md w-full p-6 overflow-y-auto"
    >
      <h2 class="text-xl font-semibold text-foreground mb-4">
        Create Local User
      </h2>
      <form
        onsubmit={(e) => {
          e.preventDefault();
          createUser();
        }}
        class="space-y-4"
        autocomplete="off"
      >
        <div>
          <label
            for="username"
            class="block text-sm font-medium text-foreground mb-2"
            >Username</label
          >
          <input
            type="text"
            bind:value={newUser.username}
            required
            class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
            placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            placeholder="Enter username"
            id="username"
          />
        </div>
        <div>
          <label
            for="password"
            class="block text-sm font-medium text-foreground mb-2"
            >Password</label
          >
          <input
            type="password"
            bind:value={newUser.password}
            required
            class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
            placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            placeholder="Enter password"
            id="password"
          />
        </div>
        <div>
          <label
            for="display_name"
            class="block text-sm font-medium text-foreground mb-2"
            >Display Name (Optional)</label
          >
          <input
            type="text"
            bind:value={newUser.display_name}
            class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
            placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            placeholder="Enter display name"
            id="display_name"
          />
        </div>
        <div>
          <label
            for="email"
            class="block text-sm font-medium text-foreground mb-2"
            >Email (Optional)</label
          >
          <input
            type="email"
            bind:value={newUser.email}
            class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
            placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            placeholder="Enter email"
            id="email"
          />
        </div>
        <div>
          <label
            for="role"
            class="block text-sm font-medium text-foreground mb-2">Role</label
          >
          <select
            bind:value={newUser.role}
            class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
            focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            id="role"
          >
            <option value="user">User</option>
            {#if isAdmin}
              <option value="admin">Admin</option>
            {/if}
          </select>
        </div>
        <div>
          <label
            for="new_permissions_container"
            class="block text-sm font-medium text-foreground">Permissions</label
          >
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
              <label
                class="flex items-start gap-2 text-sm text-foreground {canEditPermission(
                  option.value,
                )
                  ? 'cursor-pointer'
                  : 'opacity-50 cursor-not-allowed'}"
              >
                <input
                  type="checkbox"
                  checked={newUser.permissions.includes(option.value)}
                  disabled={!canEditPermission(option.value)}
                  onchange={(e) =>
                    toggleCreatePermission(
                      option.value,
                      (e.currentTarget as HTMLInputElement).checked,
                    )}
                />
                <span>
                  <span class="font-medium">{option.label}</span>
                  <span class="block text-xs text-muted-foreground"
                    >{option.description}</span
                  >
                </span>
              </label>
            {/each}
          </div>
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
    </div>
  </div>
{/if}

<!-- edit user modal -->
{#if showEditModal && editingUser}
  <div
    class="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
  >
    <div
      class="max-h-[90vh] bg-card rounded-lg border border-border max-w-md w-full p-6 overflow-y-auto"
    >
      <h2 class="text-xl font-semibold text-foreground mb-4">
        Edit User: {editingUser.username}
      </h2>
      <form
        onsubmit={(e) => {
          (e.preventDefault(), updateUser());
        }}
        class="space-y-4"
        autocomplete="off"
      >
        <div>
          <label
            for="edit_display_name"
            class="block text-sm font-medium text-foreground mb-2"
            >Display Name</label
          >
          <input
            type="text"
            bind:value={editUser.display_name}
            class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
            placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            placeholder="Enter display name"
            id="edit_display_name"
          />
        </div>
        <div>
          <label
            for="edit_email"
            class="block text-sm font-medium text-foreground mb-2">Email</label
          >
          <input
            type="email"
            bind:value={editUser.email}
            class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
            placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            placeholder="Enter email"
            id="edit_email"
          />
        </div>
        <div>
          <label
            for="edit_role"
            class="block text-sm font-medium text-foreground mb-2">Role</label
          >
          <select
            bind:value={editUser.role}
            class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            id="edit_role"
          >
            <option value="user">User</option>
            {#if isAdmin}
              <option value="admin">Admin</option>
            {/if}
          </select>
        </div>
        <div>
          <label
            for="edit_permissions_container"
            class="block text-sm font-medium text-foreground">Permissions</label
          >
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
              <label
                class="flex items-start gap-2 text-sm text-foreground
                  {canEditPermission(option.value)
                  ? 'cursor-pointer'
                  : 'opacity-50 cursor-not-allowed'}"
              >
                <input
                  type="checkbox"
                  checked={editUser.permissions.includes(option.value)}
                  disabled={!canEditPermission(option.value)}
                  onchange={(e) =>
                    toggleEditPermission(
                      option.value,
                      (e.currentTarget as HTMLInputElement).checked,
                    )}
                />
                <span>
                  <span class="font-medium">{option.label}</span>
                  <span class="block text-xs text-muted-foreground"
                    >{option.description}</span
                  >
                </span>
              </label>
            {/each}
          </div>
        </div>
        <div>
          <label
            for="edit_password"
            class="block text-sm font-medium text-foreground mb-2"
            >New Password (Optional)</label
          >
          <input
            type="password"
            bind:value={editUser.password}
            class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            placeholder="Leave blank to keep current password"
            id="edit_password"
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
    </div>
  </div>
{/if}

<!-- confirm delete alert dialog -->
<AlertDialog.Root
  open={showDeleteDialog}
  onOpenChange={(v) => (showDeleteDialog = v)}
>
  <AlertDialog.Content
    class="bg-card border border-border rounded-lg p-6 max-w-md w-full"
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
