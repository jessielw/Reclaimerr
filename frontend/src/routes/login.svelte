<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { get } from "svelte/store";
  import { auth } from "$lib/stores/auth";
  import type {
    MediaAuthProvider,
    MediaAuthProvidersResponse,
  } from "$lib/types/shared";
  import ReclaimerrSVG from "$lib/components/svgs/reclaimerr-logo-svg.svelte";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import DoorOpen from "@lucide/svelte/icons/door-open";
  import DoorClosed from "@lucide/svelte/icons/door-closed";
  import Lock from "@lucide/svelte/icons/lock";
  import LockOpen from "@lucide/svelte/icons/lock-open";
  import Server from "@lucide/svelte/icons/server";
  import PlexSVG from "$lib/components/svgs/plex-svg.svelte";
  import JellyfinSVG from "$lib/components/svgs/jellyfin-svg.svelte";
  import EmbySVG from "$lib/components/svgs/emby-svg.svelte";
  import { get_api } from "$lib/api";
  import { shuffleArray } from "$lib/utils/array";
  import { TOP_RATED_BACKDROPS } from "$lib/misc/tmdb-images";

  type LoginMethod = "local" | "media" | "sso";
  const MEDIA_SERVER_ICONS: Record<string, any> = {
    jellyfin: JellyfinSVG,
    emby: EmbySVG,
    plex: PlexSVG,
  };

  let username = $state("");
  let password = $state("");
  let error = $state("");
  let localLoading = $state(false);
  let loginHovered = $state(false);
  let loginMethod = $state<LoginMethod>("local");

  let oidcEnabled = $state(false);
  let oidcLoading = $state(false);
  let oidcHovered = $state(false);

  let mediaProviders = $state<MediaAuthProvider[]>([]);
  let mediaProviderId = $state("");
  let mediaUsername = $state("");
  let mediaPassword = $state("");
  let mediaLoading = $state(false);
  let mediaHovered = $state(false);

  const selectedMediaProvider = $derived.by(
    () =>
      mediaProviders.find(
        (provider) => String(provider.service_config_id) === mediaProviderId,
      ) ??
      mediaProviders[0] ??
      null,
  );
  const selectedMediaIsRedirect = $derived.by(
    () => selectedMediaProvider?.auth_mode === "redirect",
  );
  const mediaSignInDisabled = $derived.by(() => {
    if (mediaLoading || !selectedMediaProvider) return true;
    if (selectedMediaProvider.auth_mode === "redirect") return false;
    return !mediaUsername.trim() || !mediaPassword.trim();
  });
  const hasMediaMethods = $derived.by(() => mediaProviders.length > 0);
  const hasSsoMethod = $derived.by(() => oidcEnabled);
  const visibleMethods = $derived.by(() => {
    const methods: LoginMethod[] = ["local"];
    if (hasMediaMethods) methods.push("media");
    if (hasSsoMethod) methods.push("sso");
    return methods;
  });
  const isBusy = $derived.by(() => localLoading || mediaLoading || oidcLoading);
  const mediaMethodIconTypes = $derived.by(() => {
    const unique = new Set<string>();
    const types: string[] = [];
    for (const provider of mediaProviders) {
      const key = serviceTypeKey(provider.service_type);
      if (!key || unique.has(key)) continue;
      unique.add(key);
      types.push(key);
    }
    return types;
  });

  const TMDB_BASE_URL_ORIGINAL = "https://image.tmdb.org/t/p/original";
  const TMDB_BASE_URL_W1280 = "https://image.tmdb.org/t/p/w1280";
  const RANDOM_BACKGROUND_IMG_INTERVAL = 5000;
  let imageBaseUrl = TMDB_BASE_URL_ORIGINAL;
  let refreshInterval: number | null = null;
  let popupCheckInterval: number | null = null;
  let authPopup: Window | null = null;
  let authPopupCompleted = false;
  let authChannel: BroadcastChannel | null = null;
  let overlay: HTMLElement | null;
  let backDropUrls: string[] = [];

  let container: HTMLElement;
  let observer: ResizeObserver;

  const usernameMaxLength = $derived.by(() => {
    return username.includes("@") ? 120 : 32;
  });

  const authCompleteUrl = () => `${window.location.origin}/#/auth/complete`;

  const stopPopupTracking = () => {
    if (popupCheckInterval) {
      clearInterval(popupCheckInterval);
      popupCheckInterval = null;
    }
    authPopup = null;
  };

  const handleAuthMessage = (event: MessageEvent) => {
    if (event.origin !== window.location.origin) return;
    if (event.data?.type !== "reclaimerr-auth-complete") return;
    void finishRedirectAuth(event.data.error ?? null);
  };

  const finishRedirectAuth = async (authError: string | null) => {
    if (authPopupCompleted) return;
    authPopupCompleted = true;
    stopPopupTracking();
    mediaLoading = false;
    oidcLoading = false;

    if (authError) {
      error = authError;
      return;
    }

    await auth.init();
    if (!get(auth).isAuthenticated) {
      error = "Sign-in completed but no session was created.";
    }
  };

  const startRedirectAuth = (url: string, fallbackUrl: string) => {
    authPopupCompleted = false;
    error = "";
    stopPopupTracking();

    const width = 520;
    const height = 720;
    const left = Math.max(0, window.screenX + (window.outerWidth - width) / 2);
    const top = Math.max(0, window.screenY + (window.outerHeight - height) / 2);
    const features = [
      "popup=yes",
      `width=${width}`,
      `height=${height}`,
      `left=${Math.round(left)}`,
      `top=${Math.round(top)}`,
      "resizable=yes",
      "scrollbars=yes",
    ].join(",");

    authPopup = window.open(url, "reclaimerr-auth", features);
    if (!authPopup) {
      window.location.href = fallbackUrl;
      return;
    }

    authPopup.focus();
    popupCheckInterval = window.setInterval(() => {
      if (!authPopup || !authPopup.closed || authPopupCompleted) return;
      stopPopupTracking();
      mediaLoading = false;
      oidcLoading = false;
      error = "Sign-in window was closed before authentication completed.";
    }, 500);
  };

  const handleLocalLogin = async () => {
    error = "";
    localLoading = true;
    try {
      await auth.login(username, password);
    } catch (err: any) {
      error = err.message || "Login failed";
    } finally {
      localLoading = false;
    }
  };

  const handleMediaLogin = async () => {
    if (!selectedMediaProvider) return;
    error = "";

    if (selectedMediaProvider.auth_mode === "redirect") {
      mediaLoading = true;
      const params = new URLSearchParams({
        service_config_id: String(selectedMediaProvider.service_config_id),
        return_to: authCompleteUrl(),
      });
      const url = `/api/auth/media/plex/start?${params.toString()}`;
      startRedirectAuth(url, url);
      return;
    }

    mediaLoading = true;
    try {
      await auth.loginMedia(
        selectedMediaProvider.service_config_id,
        mediaUsername,
        mediaPassword,
      );
    } catch (err: any) {
      error = err.message || "Media sign-in failed";
    } finally {
      mediaLoading = false;
    }
  };

  const handleKeydown = (event: KeyboardEvent) => {
    if (event.key === "Enter") {
      handleLocalLogin();
    }
  };

  const handleMediaKeydown = (event: KeyboardEvent) => {
    if (event.key === "Enter") {
      handleMediaLogin();
    }
  };

  const startOidcLogin = () => {
    oidcLoading = true;
    const params = new URLSearchParams({
      return_to: authCompleteUrl(),
    });
    const url = `/api/auth/oidc/start?${params.toString()}`;
    startRedirectAuth(url, url);
  };

  const setLoginMethod = (method: LoginMethod) => {
    loginMethod = method;
    error = "";
  };

  const serviceTypeKey = (serviceType: string | null | undefined): string =>
    String(serviceType || "")
      .trim()
      .toLowerCase();

  const mediaServiceIcon = (serviceType: string) =>
    MEDIA_SERVER_ICONS[serviceTypeKey(serviceType)] ?? Server;

  const loadMediaProviders = async () => {
    try {
      const payload = await get_api<MediaAuthProvidersResponse>(
        "/api/auth/media/providers",
      );
      mediaProviders = payload.providers ?? [];
      if (mediaProviders.length === 0) {
        mediaProviderId = "";
        return;
      }

      const defaultId = payload.default_service_config_id
        ? String(payload.default_service_config_id)
        : String(mediaProviders[0].service_config_id);
      mediaProviderId = defaultId;
    } catch {
      mediaProviders = [];
      mediaProviderId = "";
    }
  };

  const updateBaseUrl = (width: number) => {
    if (width < 1920) {
      imageBaseUrl = TMDB_BASE_URL_W1280;
    } else {
      imageBaseUrl = TMDB_BASE_URL_ORIGINAL;
    }
  };

  const setRandomBackgroundImage = async () => {
    try {
      if (backDropUrls.length === 0) {
        try {
          const response = await get_api<{ backdrops: string[] | null }>(
            "/api/info/random-backdrop",
          );
          backDropUrls =
            response.backdrops ?? shuffleArray([...TOP_RATED_BACKDROPS]);
        } catch {
          backDropUrls = shuffleArray([...TOP_RATED_BACKDROPS]);
        }
      }

      const imageUrl = backDropUrls.shift();
      if (overlay) {
        overlay.style.opacity = "0";
        setTimeout(() => {
          if (!overlay) return;
          if (imageUrl) {
            overlay.style.backgroundImage = `url(${imageBaseUrl + imageUrl})`;
          }
          overlay.style.opacity = "1";
        }, 500);
      }
    } catch (err) {
      console.error("Failed to fetch background image:", err);
    }
  };

  onMount(async () => {
    if (typeof window !== "undefined") {
      const params = new URLSearchParams(window.location.search);
      const authError = params.get("auth_error");
      if (authError) {
        error = authError;
        params.delete("auth_error");
        const nextQuery = params.toString();
        const nextUrl = `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ""}${window.location.hash}`;
        window.history.replaceState({}, "", nextUrl);
      }
    }

    window.addEventListener("message", handleAuthMessage);

    try {
      authChannel = new BroadcastChannel("reclaimerr-auth");
      authChannel.onmessage = (event) => {
        if (event.data?.type !== "reclaimerr-auth-complete") return;
        void finishRedirectAuth(event.data.error ?? null);
      };
    } catch {
      authChannel = null;
    }

    try {
      const response = await fetch("/api/auth/oidc/status", {
        credentials: "include",
      });
      if (response.ok) {
        const payload = await response.json();
        oidcEnabled = Boolean(payload?.enabled);
      }
    } catch {
      oidcEnabled = false;
    }

    await loadMediaProviders();
    if (!visibleMethods.includes(loginMethod)) {
      loginMethod = visibleMethods[0] ?? "local";
    }

    overlay = document.getElementById("login-bg-overlay");
    container = (overlay?.parentElement as HTMLElement) || document.body;
    observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        updateBaseUrl(entry.contentRect.width);
        setRandomBackgroundImage();
      }
    });
    if (container) observer.observe(container);

    await setRandomBackgroundImage();
    refreshInterval = window.setInterval(
      setRandomBackgroundImage,
      RANDOM_BACKGROUND_IMG_INTERVAL,
    );
  });

  onDestroy(() => {
    if (refreshInterval) clearInterval(refreshInterval);
    stopPopupTracking();
    window.removeEventListener("message", handleAuthMessage);
    authChannel?.close();
    if (observer && container) observer.unobserve(container);
  });
</script>

<div class="dark">
  <div id="login-bg-overlay"></div>
  <div
    class="min-h-screen flex items-center justify-center bg-transparent px-4 text-foreground"
  >
    <div
      class="max-w-md min-h-[80vh] max-h-[90vh] w-full space-y-8 overflow-y-auto"
    >
      <div
        class="bg-card/90 backdrop-blur-xl rounded-lg shadow-2xl shadow-primary/10 p-8 border border-border"
      >
        {#if error}
          <div
            class="mb-4 rounded-lg border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive"
          >
            {error}
          </div>
        {/if}

        <div class="text-center mb-4">
          <div class="flex justify-center mb-4">
            <ReclaimerrSVG
              class="w-1/2 stroke-13 stroke-primary-stroke {loginHovered
                ? 'fill-primary-hover'
                : 'fill-primary'}
              duration-400 transition-colors"
            />
          </div>
          <h1 class="text-4xl font-bold text-foreground mb-2">Reclaimerr</h1>
        </div>

        {#if visibleMethods.length > 1}
          <div
            class="mb-5 grid gap-1 rounded-md border border-border bg-background/70 p-1"
            style={`grid-template-columns: repeat(${visibleMethods.length}, minmax(0, 1fr));`}
          >
            <!-- local -->
            {#if visibleMethods.includes("local")}
              <button
                type="button"
                class="flex items-center justify-center gap-2 rounded-sm px-3 py-2 text-sm cursor-pointer
                  {loginMethod === 'local'
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:text-foreground'}"
                onclick={() => setLoginMethod("local")}
                disabled={isBusy}
              >
                <DoorClosed class="size-4" />
                Local
              </button>
            {/if}

            <!-- media server -->
            {#if visibleMethods.includes("media")}
              <button
                type="button"
                class="flex items-center justify-center gap-2 rounded-sm px-3 py-2 text-sm cursor-pointer
                  {loginMethod === 'media'
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:text-foreground'}"
                onclick={() => setLoginMethod("media")}
                disabled={isBusy}
              >
                {#if mediaMethodIconTypes.length > 0}
                  <span class="flex items-center gap-1">
                    {#each mediaMethodIconTypes as serviceType}
                      {@const Icon = mediaServiceIcon(serviceType)}
                      <span class="inline-flex items-center justify-center">
                        <Icon class="size-4" />
                      </span>
                    {/each}
                  </span>
                {:else}
                  <Server class="size-4" />
                {/if}
                Media
              </button>
            {/if}

            <!-- sso -->
            {#if visibleMethods.includes("sso")}
              <button
                type="button"
                class="flex items-center justify-center gap-2 rounded-sm px-3 py-2 text-sm cursor-pointer
                  {loginMethod === 'sso'
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:text-foreground'}"
                onclick={() => setLoginMethod("sso")}
                disabled={isBusy}
              >
                <Lock class="size-4" />
                SSO
              </button>
            {/if}
          </div>
        {/if}

        <!-- handle local -->
        {#if loginMethod === "local"}
          <form
            onsubmit={(e) => {
              e.preventDefault();
              handleLocalLogin();
            }}
            class="space-y-4"
          >
            <Input
              id="username"
              type="text"
              bind:value={username}
              onkeydown={handleKeydown}
              disabled={isBusy}
              required
              class="input-hover-el"
              placeholder="Username / Email"
              autocomplete="username"
              minlength={5}
              maxlength={usernameMaxLength}
            />

            <Input
              id="password"
              type="password"
              bind:value={password}
              onkeydown={handleKeydown}
              disabled={isBusy}
              required
              class="input-hover-el"
              placeholder="Password"
              autocomplete="current-password"
              minlength={3}
              maxlength={64}
            />

            <Button
              type="submit"
              onmouseenter={() => (loginHovered = true)}
              onmouseleave={() => (loginHovered = false)}
              disabled={isBusy}
              size="lg"
              class="flex justify-center w-full py-2 px-4 bg-primary hover:bg-primary-hover
                font-medium rounded-md transition-colors focus:ring-2 focus:ring-focus-ring
                cursor-pointer"
            >
              {#if loginHovered}
                <DoorOpen class="size-6" />
              {:else}
                <DoorClosed class="size-6" />
              {/if}
              <span class="font-medium"
                >{localLoading ? "Signing in..." : "Sign In"}</span
              >
            </Button>
          </form>

          <!-- handle media server -->
        {:else if loginMethod === "media" && hasMediaMethods}
          <form
            onsubmit={(e) => {
              e.preventDefault();
              handleMediaLogin();
            }}
            class="space-y-4"
          >
            <Select.Root type="single" bind:value={mediaProviderId}>
              <Select.Trigger class="w-full bg-background/80 input-hover-el">
                {#if selectedMediaProvider}
                  {@const Icon = mediaServiceIcon(
                    selectedMediaProvider.service_type,
                  )}
                  <span class="flex items-center gap-2">
                    <Icon class="size-4 shrink-0" />
                    <span>{selectedMediaProvider.name}</span>
                  </span>
                {:else}
                  Select Media Server
                {/if}
              </Select.Trigger>
              <Select.Content>
                {#each mediaProviders as provider}
                  {@const Icon = mediaServiceIcon(provider.service_type)}
                  <Select.Item
                    value={String(provider.service_config_id)}
                    label={provider.name}
                  >
                    <span class="flex items-center gap-2">
                      <Icon class="size-4 shrink-0" />
                      <span>{provider.name}</span>
                    </span>
                  </Select.Item>
                {/each}
              </Select.Content>
            </Select.Root>

            {#if !selectedMediaIsRedirect}
              <Input
                type="text"
                bind:value={mediaUsername}
                onkeydown={handleMediaKeydown}
                disabled={isBusy}
                class="input-hover-el"
                placeholder="Media Server Username"
                autocomplete="username"
              />

              <Input
                type="password"
                bind:value={mediaPassword}
                onkeydown={handleMediaKeydown}
                disabled={isBusy}
                class="input-hover-el"
                placeholder="Media Server Password"
                autocomplete="current-password"
              />
            {/if}

            <Button
              type="submit"
              disabled={mediaSignInDisabled || localLoading}
              onmouseenter={() => (mediaHovered = true)}
              onmouseleave={() => (mediaHovered = false)}
              size="lg"
              class="flex justify-center w-full py-2 px-4 bg-primary hover:bg-primary-hover
                font-medium rounded-md transition-colors focus:ring-2 focus:ring-focus-ring
                cursor-pointer"
            >
              <Server class="size-5" />
              <span class="font-medium">
                {#if selectedMediaIsRedirect}
                  {mediaHovered ? "Continue to Plex" : "Sign In with Plex"}
                {:else}
                  {mediaLoading ? "Signing in..." : "Sign In with Media Server"}
                {/if}
              </span>
            </Button>
          </form>

          <!-- sso -->
        {:else if loginMethod === "sso" && hasSsoMethod}
          <Button
            disabled={isBusy}
            onmouseenter={() => (oidcHovered = true)}
            onmouseleave={() => (oidcHovered = false)}
            size="lg"
            class="flex justify-center w-full py-2 px-4 font-medium cursor-pointer
              bg-primary hover:bg-primary-hover transition-colors"
            onclick={startOidcLogin}
          >
            {#if oidcHovered}
              <LockOpen class="size-5" />
            {:else}
              <Lock class="size-5" />
            {/if}
            <span class="font-medium">
              {oidcLoading ? "Opening SSO..." : "Sign In with SSO"}
            </span>
          </Button>
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  #login-bg-overlay {
    position: fixed;
    inset: 0;
    z-index: -1;
    background-color: color-mix(
      in oklch,
      var(--background) 82%,
      var(--primary) 18%
    );
    background-size: cover;
    background-repeat: no-repeat;
    background-position: center center;
    opacity: 1;
    transition: opacity 0.5s ease-out;
  }
</style>
