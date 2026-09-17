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
  type RedirectAuthProvider = "plex" | "oidc";

  const AUTH_POLL_INTERVAL_MS = 2000;
  // Matches PLEX_PENDING_AUTH_TTL on the backend.
  const AUTH_FLOW_TIMEOUT_MS = 10 * 60 * 1000;
  // The popup's /start hop has to reach plex.tv before a flow exists to poll for,
  // so early "expired" answers mean "not yet", not "gone".
  const AUTH_FLOW_GRACE_MS = 35 * 1000;
  const AUTH_WINDOW_PATH = "/api/auth/signin-window";
  // If the window never says it is ready, send it on anyway rather than leaving
  // it sitting on the spinner.
  const AUTH_HANDOFF_FALLBACK_MS = 2000;
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
  let authPopup: Window | null = null;
  let authPopupCompleted = false;
  let authChannel: BroadcastChannel | null = null;
  let authPollInterval: number | null = null;
  let authPollDeadline = 0;
  let authProvider: RedirectAuthProvider | null = null;
  let authFlowSeen = false;
  let authFlowGraceUntil = 0;
  let authPopupUrl = "";
  let authStartUrl = "";
  let authRedirectUrl = "";
  let authHandedOff = false;
  let authHandoffTimer: number | null = null;
  let authWaiting = $state(false);
  let overlay: HTMLElement | null;
  let backDropUrls: string[] = [];

  let container: HTMLElement;
  let observer: ResizeObserver;

  const usernameMaxLength = $derived.by(() => {
    return username.includes("@") ? 120 : 32;
  });

  const authCompleteUrl = () => `${window.location.origin}/#/auth/complete`;

  const stopAuthPolling = () => {
    clearAuthHandoffTimer();
    if (authPollInterval) {
      clearInterval(authPollInterval);
      authPollInterval = null;
    }
  };

  const closeAuthPopup = () => {
    try {
      authPopup?.close();
    } catch {
      // Cross-Origin-Opener-Policy on the identity provider can sever this handle.
      // The callback page closes itself too, so this is only a nicety.
    }
    authPopup = null;
  };

  // The popup tells us it is done, but it is never what proves it - it may not be
  // able to reach us at all. Treat a message as a reason to poll now rather than
  // waiting out the interval.
  const handleAuthNudge = (authError: string | null) => {
    if (authPopupCompleted) return;
    if (authError) {
      void finishRedirectAuth(authError);
      return;
    }
    void pollRedirectAuth();
  };

  const handleAuthMessage = (event: MessageEvent) => {
    if (event.origin !== window.location.origin) return;
    if (event.data?.type === "reclaimerr-auth-window-ready") {
      handoffAuthPopup();
      return;
    }
    if (event.data?.type !== "reclaimerr-auth-complete") return;
    handleAuthNudge(event.data.error ?? null);
  };

  // The sign-in window opens on a local spinner and asks us to send it onward, so
  // the destination never has to be handed to a page as a parameter. Both windows
  // are still same-origin at this point, which is the one moment in this flow
  // where driving the popup is dependable.
  const handoffAuthPopup = () => {
    if (authHandedOff || !authPopup || !authStartUrl) return;
    authHandedOff = true;
    clearAuthHandoffTimer();
    try {
      authPopup.location.href = authStartUrl;
    } catch {
      // Cannot drive the window - finish in this tab instead of stranding them.
      closeAuthPopup();
      window.location.href = authRedirectUrl;
    }
  };

  const clearAuthHandoffTimer = () => {
    if (authHandoffTimer) {
      clearTimeout(authHandoffTimer);
      authHandoffTimer = null;
    }
  };

  const armAuthHandoff = () => {
    authHandedOff = false;
    clearAuthHandoffTimer();
    authHandoffTimer = window.setTimeout(
      handoffAuthPopup,
      AUTH_HANDOFF_FALLBACK_MS,
    );
  };

  const pollRedirectAuth = async () => {
    if (authPopupCompleted) return;

    if (authProvider !== "plex") {
      // OIDC completes server-side at its callback and the session cookie is
      // shared with this window, so asking who we are is the reliable signal.
      await auth.init();
      if (get(auth).isAuthenticated) await finishRedirectAuth(null);
      return;
    }

    let payload: { status?: string; message?: string } | null = null;
    try {
      const response = await fetch("/api/auth/media/plex/poll", {
        credentials: "include",
      });
      if (!response.ok) return;
      payload = await response.json();
    } catch {
      // Transient - keep polling until the deadline.
      return;
    }

    if (payload?.status === "pending") {
      authFlowSeen = true;
      return;
    }
    if (payload?.status === "authenticated") {
      await finishRedirectAuth(null);
      return;
    }
    if (payload?.status === "error") {
      await finishRedirectAuth(payload.message || "Plex sign-in failed.");
      return;
    }
    if (payload?.status === "expired") {
      if (!authFlowSeen && Date.now() < authFlowGraceUntil) return;
      // It may still have completed some other way - check before erroring out.
      await auth.init();
      await finishRedirectAuth(
        get(auth).isAuthenticated
          ? null
          : "Plex sign-in expired. Please try again.",
      );
    }
  };

  const startAuthPolling = (provider: RedirectAuthProvider) => {
    stopAuthPolling();
    authProvider = provider;
    authFlowSeen = false;
    authFlowGraceUntil = Date.now() + AUTH_FLOW_GRACE_MS;
    authPollDeadline = Date.now() + AUTH_FLOW_TIMEOUT_MS;
    authPollInterval = window.setInterval(() => {
      if (authPopupCompleted) {
        stopAuthPolling();
        return;
      }
      if (Date.now() >= authPollDeadline) {
        void finishRedirectAuth("Sign-in timed out. Please try again.");
        return;
      }
      void pollRedirectAuth();
    }, AUTH_POLL_INTERVAL_MS);
  };

  const finishRedirectAuth = async (authError: string | null) => {
    if (authPopupCompleted) return;
    authPopupCompleted = true;
    stopAuthPolling();
    closeAuthPopup();
    authWaiting = false;
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

  const cancelRedirectAuth = () => {
    authPopupCompleted = true;
    stopAuthPolling();
    closeAuthPopup();
    authWaiting = false;
    mediaLoading = false;
    oidcLoading = false;
  };

  // Popups on phones become extra tabs at best and are dropped at worst.
  const prefersFullPageAuth = () =>
    window.matchMedia?.("(pointer: coarse)").matches || window.innerWidth < 640;

  const openAuthPopup = (url: string): Window | null => {
    const screen = window.screen as Screen & {
      availLeft?: number;
      availTop?: number;
    };
    const availWidth = screen?.availWidth ?? 1280;
    const availHeight = screen?.availHeight ?? 900;
    const availLeft = screen?.availLeft ?? 0;
    const availTop = screen?.availTop ?? 0;

    // Plex's sign-in page drops to a cramped narrow layout well below ~800px,
    // which is what forced people to resize the window by hand.
    const width = Math.round(Math.min(860, Math.max(520, availWidth - 80)));
    const height = Math.round(Math.min(940, Math.max(560, availHeight - 80)));

    // Deliberately not clamped to 0: screenLeft is negative when the browser sits
    // on a display left of the primary one, and clamping threw the popup onto the
    // other monitor, where it looked like it had opened in the background.
    const originLeft = window.screenLeft ?? window.screenX ?? 0;
    const originTop = window.screenTop ?? window.screenY ?? 0;
    const centeredLeft = originLeft + (window.outerWidth - width) / 2;
    const centeredTop = originTop + (window.outerHeight - height) / 2;
    const left = Math.round(
      Math.min(
        Math.max(centeredLeft, availLeft),
        availLeft + availWidth - width,
      ),
    );
    const top = Math.round(
      Math.min(
        Math.max(centeredTop, availTop),
        availTop + availHeight - height,
      ),
    );

    const features = [
      "popup=yes",
      `width=${width}`,
      `height=${height}`,
      `left=${left}`,
      `top=${top}`,
      "resizable=yes",
      "scrollbars=yes",
    ].join(",");

    const popup = window.open(url, "reclaimerr-auth", features);
    if (!popup) return null;

    // Some window managers hand focus straight back to the opener, so ask more
    // than once. Nothing can force it, which is why the UI also offers a reopen.
    const focusPopup = () => {
      try {
        popup.focus();
      } catch {
        // handle may already be severed
      }
    };
    focusPopup();
    requestAnimationFrame(focusPopup);
    window.setTimeout(focusPopup, 250);
    return popup;
  };

  const reopenAuthPopup = () => {
    if (!authPopupUrl) return;
    closeAuthPopup();
    // Reopening starts a fresh flow on the backend, so give it the same grace
    // period a first attempt gets before an "expired" answer counts.
    authFlowSeen = false;
    authFlowGraceUntil = Date.now() + AUTH_FLOW_GRACE_MS;
    authPopup = openAuthPopup(authPopupUrl);
    if (authPopup) armAuthHandoff();
  };

  const startRedirectAuth = (
    provider: RedirectAuthProvider,
    baseUrl: string,
  ) => {
    authPopupCompleted = false;
    error = "";
    stopAuthPolling();
    closeAuthPopup();

    // The mode is stated outright rather than inferred later from browser state:
    // window.name and window.opener are both wiped by the cross-origin round trip.
    authRedirectUrl = `${baseUrl}&mode=redirect`;
    if (prefersFullPageAuth()) {
      window.location.href = authRedirectUrl;
      return;
    }

    // Opening straight at the start route left the window blank while the server
    // talked to the provider. This paints a spinner first, and the browser keeps
    // showing it until the provider's page is ready to take over.
    authStartUrl = `${baseUrl}&mode=popup`;
    authPopupUrl = AUTH_WINDOW_PATH;
    authPopup = openAuthPopup(authPopupUrl);
    if (!authPopup) {
      window.location.href = authRedirectUrl;
      return;
    }

    armAuthHandoff();
    authWaiting = true;
    startAuthPolling(provider);
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
      startRedirectAuth(
        "plex",
        `/api/auth/media/plex/start?${params.toString()}`,
      );
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
    startRedirectAuth("oidc", `/api/auth/oidc/start?${params.toString()}`);
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
        handleAuthNudge(event.data.error ?? null);
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
    stopAuthPolling();
    closeAuthPopup();
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

        {#if authWaiting}
          <div
            class="mb-4 space-y-2 rounded-lg border border-border bg-muted/40 p-3 text-sm"
          >
            <p class="text-muted-foreground">
              Waiting for you to finish signing in...
            </p>
            <div class="flex flex-wrap items-center gap-x-4 gap-y-1">
              <button
                type="button"
                class="cursor-pointer text-primary underline underline-offset-2"
                onclick={reopenAuthPopup}
              >
                Don't see the sign-in window? Open it again
              </button>
              <button
                type="button"
                class="cursor-pointer text-muted-foreground underline underline-offset-2"
                onclick={cancelRedirectAuth}
              >
                Cancel
              </button>
            </div>
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
