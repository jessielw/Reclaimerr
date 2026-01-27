<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import ErrorBox from "$lib/components/ErrorBox.svelte";
  import Spinner from "$lib/components/ui/spinner.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { toast } from "svelte-sonner";
  import type { UserProfile } from "$lib/types/shared";
  import Save from "@lucide/svelte/icons/save";
  import Pencil from "@lucide/svelte/icons/pencil";

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
      await post_api("/api/account/avatar", formData);
      avatarFile = null;
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

<div class="p-8">
  <div class="max-w-7xl mx-auto">
    <!-- header -->
    <div class="mb-2">
      <h1 class="text-3xl font-bold text-foreground mb-2">Account Settings</h1>
      <p class="text-muted-foreground">Manage your profile and preferences</p>
    </div>

    <ErrorBox error={profileError} />

    {#if loading}
      <div class="p-8 text-center text-muted-foreground">
        <Spinner size="lg" class="text-primary" />
        <p class="mt-4">Loading profile...</p>
      </div>
    {:else if profile}
      <div class="space-y-6">
        <!-- avatar Section -->
        <div class="bg-card rounded-lg border border-border p-6">
          <h2 class="text-xl font-semibold text-foreground mb-4">
            Profile Picture
          </h2>
          <div class="flex items-center gap-6">
            <div class="relative w-32 h-32">
              {#if avatarPreview}
                <img
                  src={avatarPreview}
                  alt="Avatar"
                  class="w-32 h-32 rounded-full object-cover
                    border-4 {avatarInputState === AVATAR_SAVE
                    ? 'border-destructive'
                    : 'border-primary'}"
                />
              {:else}
                <div
                  class="w-32 h-32 rounded-full bg-primary text-primary-foreground flex items-center
                    justify-center text-4xl font-bold border-4 border-primary"
                >
                  {profile.username.charAt(0).toUpperCase()}
                </div>
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
              <input
                type="text"
                value={profile.username}
                disabled
                class="w-full px-4 py-2 bg-muted border border-border rounded-lg text-muted-foreground
                  cursor-not-allowed"
                id="username"
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
              <input
                type="text"
                bind:value={profileForm.display_name}
                class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
                  placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring
                  focus:border-transparent"
                placeholder="Your display name"
                id="display_name"
                disabled={profileUpdating}
              />
            </div>

            <div>
              <label
                for="email"
                class="block text-sm font-medium text-foreground mb-2"
              >
                Email
              </label>
              <input
                type="email"
                bind:value={profileForm.email}
                class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
                  placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring
                  focus:border-transparent"
                placeholder="your.email@example.com"
                id="email"
                disabled={profileUpdating}
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
              <input
                type="password"
                bind:value={passwordForm.current_password}
                required
                class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
                  placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring
                  focus:border-transparent"
                placeholder="Enter current password"
                id="current_password"
                disabled={passwordUpdating}
              />
            </div>

            <div>
              <label
                for="new_password"
                class="block text-sm font-medium text-foreground mb-2"
              >
                New Password
              </label>
              <input
                type="password"
                bind:value={passwordForm.new_password}
                required
                class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
                  placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring
                  focus:border-transparent"
                placeholder="Enter new password"
                id="new_password"
                disabled={passwordUpdating}
              />
            </div>

            <div>
              <label
                for="confirm_password"
                class="block text-sm font-medium text-foreground mb-2"
              >
                Confirm New Password
              </label>
              <input
                type="password"
                bind:value={passwordForm.confirm_password}
                required
                class="w-full px-4 py-2 bg-background border border-input rounded-lg text-foreground
                  placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring
                  focus:border-transparent"
                placeholder="Confirm new password"
                id="confirm_password"
                disabled={passwordUpdating}
              />
            </div>

            <div
              class="flex items-center pt-4 border-t border-border justify-end"
            >
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
  </div>
</div>
