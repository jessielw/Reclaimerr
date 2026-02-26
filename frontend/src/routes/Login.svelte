<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { auth } from "$lib/stores/auth";
  import ReclaimerrSVG from "$lib/components/svgs/ReclaimerrLogoSVG.svelte";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import DoorOpen from "@lucide/svelte/icons/door-open";
  import DoorClosed from "@lucide/svelte/icons/door-closed";
  import { get_api } from "$lib/api";

  let username = $state("");
  let password = $state("");
  let error = $state("");
  let loading = $state(false);
  let loginHovered = $state(false);

  // TMDB image base URLs for different sizes
  const TMDB_BASE_URL_ORIGINAL = "https://image.tmdb.org/t/p/original";
  const TMDB_BASE_URL_W1280 = "https://image.tmdb.org/t/p/w1280";
  const RANDOM_BACKGROUND_IMG_INTERVAL = 5000;
  let imageBaseUrl = TMDB_BASE_URL_ORIGINAL;
  let refreshInterval: number | null = null;
  let overlay: HTMLElement | null;
  let backDropUrls: string[] = [];

  // responsive image base URL logic
  let container: HTMLElement;
  let observer: ResizeObserver;

  // dynamically set max length based on presence of '@'
  const usernameMaxLength = $derived.by(() => {
    return username.includes("@") ? 120 : 32;
  });

  // handle login form submission
  const handleLogin = async () => {
    error = "";
    loading = true;

    try {
      await auth.login(username, password);
      // successfully logged in, auth store will update and App.svelte will show dashboard
    } catch (err: any) {
      error = err.message || "Login failed";
    } finally {
      loading = false;
    }
  };

  // allow pressing Enter to submit the form
  const handleKeydown = (event: KeyboardEvent) => {
    if (event.key === "Enter") {
      handleLogin();
    }
  };

  // update image base URL based on container width
  const updateBaseUrl = (width: number) => {
    if (width < 1920) {
      imageBaseUrl = TMDB_BASE_URL_W1280;
    } else {
      imageBaseUrl = TMDB_BASE_URL_ORIGINAL;
    }
  };

  // fetch and set a random background image
  const setRandomBackgroundImage = async () => {
    try {
      // if we already have backdrop URLs, just pick the next one to avoid hitting the API too often
      if (backDropUrls.length === 0) {
        const response = await get_api<{ backdrops: string[] | null }>(
          "/api/info/random-backdrop",
        );
        if (response.backdrops) {
          backDropUrls = response.backdrops;
        } else {
          return;
        }
      }

      // pick an image URL and remove it from the list
      const imageUrl = backDropUrls.shift();

      // update background image
      if (overlay) {
        overlay.style.opacity = "0";
        setTimeout(() => {
          if (!overlay) return;
          if (imageUrl) {
            overlay.style.backgroundImage = `url(${imageBaseUrl + imageUrl})`;
          }
          overlay.style.opacity = "1";
        }, 500); // matches transition duration in CSS for smooth fade
      }
    } catch (err) {
      console.error("Failed to fetch background image:", err);
    }
  };

  onMount(async () => {
    overlay = document.getElementById("login-bg-overlay");
    container = (overlay?.parentElement as HTMLElement) || document.body;

    // set up ResizeObserver
    observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        updateBaseUrl(entry.contentRect.width);
        setRandomBackgroundImage(); // update image when size changes
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
    if (observer && container) observer.unobserve(container);
  });
</script>

<div id="login-bg-overlay"></div>
<div
  class="dark min-h-screen flex items-center justify-center bg-transparent px-4"
>
  <div class="max-w-md w-full space-y-8">
    <!-- login form -->
    <div
      class="bg-black/70 backdrop-blur-sm rounded-lg shadow-xl p-8 border border-primary"
    >
      {#if error}
        <div
          class="mb-4 p-3 bg-red-900/50 border border-red-700 rounded text-red-200 text-sm"
        >
          {error}
        </div>
      {/if}

      <form
        onsubmit={(e) => {
          e.preventDefault();
          handleLogin();
        }}
        class="space-y-4"
      >
        <!-- logo/title -->
        <div class="text-center">
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

        <!-- username input -->
        <Input
          id="username"
          type="text"
          bind:value={username}
          onkeydown={handleKeydown}
          disabled={loading}
          required
          class="w-full px-3 py-2 bg-gray-950/60! border border-gray-700 input-hover-el
            rounded-md text-white placeholder-gray-500"
          placeholder="Username / Email"
          autocomplete="username"
          minlength={5}
          maxlength={usernameMaxLength}
        />

        <!-- password input -->
        <Input
          id="password"
          type="password"
          bind:value={password}
          onkeydown={handleKeydown}
          disabled={loading}
          required
          class="w-full px-3 py-2 bg-gray-950/60! border border-gray-700 input-hover-el
            rounded-md text-white placeholder-gray-500"
          placeholder="Password"
          autocomplete="current-password"
          minlength={8}
          maxlength={64}
        />

        <Button
          type="submit"
          onmouseenter={() => (loginHovered = true)}
          onmouseleave={() => (loginHovered = false)}
          disabled={loading}
          size="lg"
          class="flex justify-center w-full py-2 px-4 bg-primary hover:bg-primary-hover 
            text-white font-medium rounded-md transition-colors focus:ring-2 focus:ring-focus-ring 
            cursor-pointer"
        >
          {#if loginHovered}
            <DoorOpen class="size-6" />
          {:else}
            <DoorClosed class="size-6" />
          {/if}
          <span class="font-medium"
            >{loading ? "Signing in..." : "Sign In"}</span
          >
        </Button>
      </form>
    </div>
  </div>
</div>

<style>
  #login-bg-overlay {
    position: fixed;
    inset: 0;
    z-index: -1;
    background: linear-gradient(135deg, #18181b 60%, #23272f 100%);
    background-size: cover;
    background-repeat: no-repeat;
    background-position: center center;
    opacity: 1;
    transition: opacity 0.5s ease-out;
  }
</style>
