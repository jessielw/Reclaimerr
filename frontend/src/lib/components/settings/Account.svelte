<script lang="ts">
  import { onMount } from "svelte";
  import { auth } from "$lib/stores/auth";
  import type { Component } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import ErrorBox from "$lib/components/ErrorBox.svelte";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { toast } from "svelte-sonner";
  import type { UserProfile } from "$lib/types/shared";
  import Save from "@lucide/svelte/icons/save";
  import Pencil from "@lucide/svelte/icons/pencil";
  import { Input } from "$lib/components/ui/input/index.js";
  import * as Avatar from "$lib/components/ui/avatar/index.js";
  import * as Tooltip from "$lib/components/ui/tooltip/index.js";
  import Info from "@lucide/svelte/icons/info";

  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  // avatar input states
  const AVATAR_IDLE = 0;
  const AVATAR_UPLOADING = 1;
  const AVATAR_SAVE = 2;

  let avatarInputState = $state(AVATAR_IDLE);
  let loading = $state(false);
  let profile: UserProfile | null = $state(null);
  let profileUpdating = $state(false);
  let passwordUpdating = $state(false);
  let profileError = $state("");

  // profile form
  let profileForm = $state({
    display_name: "",
    email: "",
  });

  // password form
  let passwordForm = $state({
    current_password: "",
    new_password: "",
    confirm_password: "",
  });

  // avatar upload
  let avatarFile: File | null = $state(null);
  let avatarPreview: string | null = $state(null);
  let avatarInputEl: HTMLInputElement | null = $state(null);

  // load user profile
  async function loadProfile() {
    try {
      loading = true;
      profileError = "";
      profile = await get_api<UserProfile>("/api/account/me");
      profileForm.display_name = profile.display_name || "";
      profileForm.email = profile.email || "";
      avatarPreview = profile.avatar_url;
    } catch (err: any) {
      profileError = err.message;
    } finally {
      loading = false;
    }
  }

  // update profile information
  async function updateProfileInfo() {
    try {
      profileUpdating = true;
      const response: {
        message: string;
        email: string | null;
        display_name: string | null;
      } = await post_api("/api/account/me", profileForm);
      profileForm = {
        display_name: response.display_name || "",
        email: response.email || "",
      };
      toast.success(response.message);
    } catch (err: any) {
      toast.error(`Error updating profile: ${err.message}`);
    } finally {
      profileUpdating = false;
    }
  }

  // update password
  async function updatePassword() {
    try {
      passwordUpdating = true;

      if (
        passwordForm.new_password.trim() !==
        passwordForm.confirm_password.trim()
      ) {
        toast.warning("New passwords do not match");
        return;
      }

      if (passwordForm.new_password.trim().length < 8) {
        toast.warning("Password must be at least 8 characters");
        return;
      }

      const response: {
        message: string;
      } = await post_api("/api/account/change-password", {
        current_password: passwordForm.current_password,
        new_password: passwordForm.new_password,
      });

      // reset form
      passwordForm = {
        current_password: "",
        new_password: "",
        confirm_password: "",
      };

      toast.success(response.message);
    } catch (err: any) {
      toast.error(`Error changing password: ${err.message}`);
    } finally {
      passwordUpdating = false;
    }
  }

  // handle avatar selection
  function handleAvatarSelect(event: Event) {
    const input = event.target as HTMLInputElement;
    if (input.files && input.files[0]) {
      avatarFile = input.files[0];
      const reader = new FileReader();
      reader.onload = (e) => {
        avatarPreview = e.target?.result as string;
      };
      reader.readAsDataURL(avatarFile);
      avatarInputState = AVATAR_SAVE;
      toast.message("Avatar selected. Click the save icon to upload.");
    }
  }

  // upload avatar
  async function uploadAvatar() {
    if (!avatarFile) return;
    avatarInputState = AVATAR_UPLOADING;
    try {
      profileError = "";
      const formData = new FormData();
      formData.append("avatar", avatarFile);
      const response: { message: string; path: string } = await post_api(
        "/api/account/avatar",
        formData,
      );
      avatarFile = null;

      // update auth user data to reflect new avatar immediately
      const curUser = $auth.user;
      if (curUser) {
        auth.updateUser({
          ...curUser,
          // '/avatars/' prefix is added by backend, so we can directly use the returned path
          avatar_url: `/avatars/${response.path}`,
        });
      }

      toast.success("Avatar uploaded successfully");
    } catch (err: any) {
      profileError = err.message;
      toast.error(`Error uploading avatar: ${err.message}`);
    } finally {
      avatarInputState = AVATAR_IDLE;
    }
  }

  // load profile on mount
  onMount(() => {
    loadProfile();
  });
</script>

<ErrorBox error={profileError} />

{#if loading}
  <div class="p-8 text-center text-muted-foreground">
    <Spinner size="lg" class="text-primary" />
    <p class="mt-4">Loading profile...</p>
  </div>
{:else if profile}
  <div class="space-y-6">
    <!-- header -->
    <div class="flex flex-col mb-3">
      <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
        {console.log(svgIcon)}
        {#if svgIcon}
          {@const Icon = svgIcon}
          <Icon class="size-5" aria-hidden="true" />
        {/if}
        <span class="align-middle">Account</span>
      </h2>
      <p class="text-sm text-muted-foreground mt-1">
        Manage your account settings
      </p>
    </div>

    <!-- avatar Section -->
    <div class="bg-card rounded-lg border border-border p-6">
      <h2 class="text-xl font-semibold text-foreground mb-4">
        Profile Picture
        <span class="ml-1">
          <Tooltip.Root>
            <Tooltip.Trigger>
              <Info class="size-4 text-muted-foreground cursor-help" />
            </Tooltip.Trigger>
            <Tooltip.Content>
              <p>Max file size: 5MB</p>
            </Tooltip.Content>
          </Tooltip.Root>
        </span>
      </h2>
      <div class="flex items-center gap-6">
        <div class="relative w-32 h-32">
          {#if avatarPreview}
            <Avatar.Root
              class="w-32 h-32 border-4 {avatarInputState === AVATAR_SAVE
                ? 'border-destructive'
                : 'border-primary'}"
            >
              <Avatar.Image src={avatarPreview} alt="Avatar" />
              <Avatar.Fallback
                >{profile.username.charAt(0).toUpperCase()}</Avatar.Fallback
              >
            </Avatar.Root>
          {:else}
            <Avatar.Root class="w-32 h-32 text-4xl font-bold">
              <Avatar.Fallback class="bg-primary">
                {profile.username.charAt(0).toUpperCase()}
              </Avatar.Fallback>
            </Avatar.Root>
          {/if}
          <!-- edit avatar icon button -->
          <label class="absolute bottom-2 right-2 cursor-pointer">
            <Button
              size="icon-sm"
              type="button"
              variant="secondary"
              class="cursor-pointer bg-secondary/75 hover:bg-secondary/90"
              disabled={avatarInputState === AVATAR_UPLOADING}
              onclick={() =>
                avatarInputState === AVATAR_SAVE
                  ? uploadAvatar()
                  : avatarInputEl && avatarInputEl.click()}
            >
              {#if avatarInputState === AVATAR_UPLOADING}
                <Spinner size="sm" class="text-primary-foreground" />
              {:else if avatarInputState === AVATAR_SAVE}
                <Save class="size-3/5" />
              {:else}
                <Pencil class="size-1/2" />
              {/if}
            </Button>
          </label>
          <input
            type="file"
            accept="image/*"
            onchange={handleAvatarSelect}
            class="hidden"
            id="avatar-upload"
            disabled={avatarInputState === AVATAR_UPLOADING}
            bind:this={avatarInputEl}
          />
        </div>
      </div>
    </div>

    <!-- profile information -->
    <div class="bg-card rounded-lg border border-border p-6">
      <h2 class="text-xl font-semibold text-foreground mb-4">
        Profile Information
      </h2>
      <form
        onsubmit={(e) => {
          e.preventDefault();
          updateProfileInfo();
        }}
        class="space-y-4"
        autocomplete="off"
      >
        <div>
          <label
            for="username"
            class="block text-sm font-medium text-foreground mb-2"
          >
            Username
          </label>
          <Input
            type="text"
            value={profile.username}
            disabled
            class="bg-muted text-muted-foreground cursor-not-allowed"
            id="username"
            minlength={5}
            maxlength={32}
          />
          <p class="mt-1 text-xs text-muted-foreground">
            Username cannot be changed
          </p>
        </div>

        <div>
          <label
            for="display_name"
            class="block text-sm font-medium text-foreground mb-2"
          >
            Display Name
          </label>
          <Input
            type="text"
            bind:value={profileForm.display_name}
            class="input-hover-el"
            placeholder="Your display name"
            id="display_name"
            disabled={profileUpdating}
            minlength={3}
            maxlength={32}
          />
        </div>

        <div>
          <label
            for="email"
            class="block text-sm font-medium text-foreground mb-2"
          >
            Email
          </label>
          <Input
            type="email"
            bind:value={profileForm.email}
            class="input-hover-el"
            placeholder="your.email@example.com"
            id="email"
            disabled={profileUpdating}
            minlength={5}
            maxlength={120}
          />
        </div>

        <div
          class="flex items-center justify-between pt-4 border-t border-border"
        >
          <div class="text-sm text-muted-foreground">
            <span class="font-medium text-foreground">Role:</span>
            <span class="capitalize">{profile.role}</span>
          </div>
          <Button
            type="submit"
            size="icon"
            class="hover cursor-pointer"
            disabled={profileUpdating}
          >
            {#if profileUpdating}
              <Spinner />
            {:else}
              <Save strokeWidth={1.5} class="size-3/5" />
            {/if}
          </Button>
        </div>
      </form>
    </div>

    <!-- password change -->
    <div class="bg-card rounded-lg border border-border p-6">
      <h2 class="text-xl font-semibold text-foreground mb-4">
        Change Password
      </h2>
      <form
        onsubmit={(e) => {
          e.preventDefault();
          updatePassword();
        }}
        class="space-y-4"
        autocomplete="off"
      >
        <div>
          <label
            for="current_password"
            class="block text-sm font-medium text-foreground mb-2"
          >
            Current Password
          </label>
          <Input
            type="password"
            bind:value={passwordForm.current_password}
            required
            class="input-hover-el"
            placeholder="Enter current password"
            id="current_password"
            disabled={passwordUpdating}
            minlength={8}
            maxlength={64}
          />
        </div>

        <div>
          <label
            for="new_password"
            class="block text-sm font-medium text-foreground mb-2"
          >
            New Password
          </label>
          <Input
            type="password"
            bind:value={passwordForm.new_password}
            required
            class="input-hover-el"
            placeholder="Enter new password"
            id="new_password"
            disabled={passwordUpdating}
            minlength={8}
            maxlength={64}
          />
        </div>

        <div>
          <label
            for="confirm_password"
            class="block text-sm font-medium text-foreground mb-2"
          >
            Confirm New Password
          </label>
          <Input
            type="password"
            bind:value={passwordForm.confirm_password}
            required
            class="input-hover-el"
            placeholder="Confirm new password"
            id="confirm_password"
            disabled={passwordUpdating}
            minlength={8}
            maxlength={64}
          />
        </div>

        <div class="flex items-center pt-4 border-t border-border justify-end">
          <Button
            type="submit"
            size="icon"
            class="hover cursor-pointer"
            disabled={passwordUpdating}
          >
            {#if passwordUpdating}
              <Spinner />
            {:else}
              <Save strokeWidth={1.5} class="size-3/5" />
            {/if}
          </Button>
        </div>
      </form>
    </div>

    <!-- account info -->
    <div class="bg-card rounded-lg border border-border p-6">
      <h2 class="text-xl font-semibold text-foreground mb-4">
        Account Information
      </h2>
      <div class="space-y-3 text-sm">
        <div class="flex justify-between">
          <span class="text-muted-foreground">Account Created:</span>
          <span class="text-foreground">
            {new Date(profile.created_at).toLocaleDateString("en-US", {
              year: "numeric",
              month: "long",
              day: "numeric",
            })}
          </span>
        </div>
        <div class="flex justify-between">
          <span class="text-muted-foreground">Role:</span>
          <span class="text-foreground">{profile.role}</span>
        </div>
      </div>
    </div>
  </div>
{/if}
