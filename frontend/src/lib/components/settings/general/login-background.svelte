<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, put_api, post_api, delete_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import { toast } from "svelte-sonner";
  import type {
    LoginBackground,
    LoginBackgroundMode,
  } from "$lib/types/login-background";

  const backgroundSourceLabels: Record<LoginBackgroundMode, string> = {
    default: "Default rotating backgrounds",
    upload: "Uploaded image",
    urls: "Custom image URLs",
  };

  const endpoint = "/api/settings/general/login-background";
  let loading = $state(true);
  let loadFailed = $state(false);
  let busy = $state(false);
  let mode = $state<LoginBackgroundMode>("default");
  let urlsText = $state("");
  let interval = $state<number | undefined>(5);
  let imageUrl = $state<string | null>(null);
  let failedUrls = $state<string[]>([]);
  let error = $state("");
  const urls = $derived([
    ...new Set(
      urlsText
        .split(/\r?\n/)
        .map((url) => url.trim())
        .filter(Boolean),
    ),
  ]);
  const previews = $derived(
    mode === "upload"
      ? imageUrl
        ? [imageUrl]
        : []
      : mode === "urls"
        ? urls.filter(validUrl).slice(0, 50)
        : [],
  );

  function validUrl(value: string) {
    try {
      const url = new URL(value);
      return (
        ["https:", "http:"].includes(url.protocol) &&
        !url.username &&
        !url.password &&
        value.length <= 4096 &&
        !/\s/.test(value)
      );
    } catch {
      return false;
    }
  }

  async function load() {
    loading = true;
    loadFailed = false;
    try {
      const config = await get_api<LoginBackground>(endpoint);
      mode = config.mode;
      urlsText = config.urls.join("\n");
      interval = config.interval_seconds;
      imageUrl = config.image_url;
    } catch {
      loadFailed = true;
    } finally {
      loading = false;
    }
  }
  onMount(load);

  async function save() {
    error = "";
    if (
      mode === "urls" &&
      (!urls.length || urls.length > 50 || urls.some((url) => !validUrl(url)))
    ) {
      error =
        "Enter 1–50 direct HTTP or HTTPS image URLs, one per line, without credentials.";
      return;
    }
    if (mode === "upload" && !imageUrl) {
      error = "Upload an image first.";
      return;
    }
    if (
      !interval ||
      !Number.isInteger(interval) ||
      interval < 5 ||
      interval > 300
    ) {
      error = "Choose a rotation interval from 5 to 300 seconds.";
      return;
    }
    busy = true;
    try {
      await put_api(endpoint, { mode, urls, interval_seconds: interval });
      toast.success("Login background saved");
    } catch (err) {
      error =
        err instanceof Error ? err.message : "Could not save login background";
    } finally {
      busy = false;
    }
  }

  async function upload(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    error = "";
    if (file.size > 10 * 1024 * 1024) {
      error = "Image must be 10 MB or smaller.";
      input.value = "";
      return;
    }
    busy = true;
    try {
      const data = new FormData();
      data.append("image", file);
      const config = await post_api<LoginBackground>(endpoint + "/image", data);
      imageUrl = config.image_url;
      failedUrls = [];
      toast.success(
        "Image uploaded. Save login background to apply your selection.",
      );
    } catch (err) {
      error = err instanceof Error ? err.message : "Could not upload image";
    } finally {
      busy = false;
      input.value = "";
    }
  }

  async function remove() {
    busy = true;
    error = "";
    try {
      const config = await delete_api<LoginBackground>(endpoint + "/image");
      imageUrl = config.image_url;
      if (mode === "upload") mode = "default";
      toast.success("Uploaded background removed");
    } catch (err) {
      error = err instanceof Error ? err.message : "Could not remove image";
    } finally {
      busy = false;
    }
  }
</script>

<section
  class="bg-muted/50 border rounded-lg p-4 shadow-sm space-y-4"
  aria-labelledby="login-background-heading"
>
  <div>
    <h3 id="login-background-heading" class="font-semibold text-foreground">
      Login background
    </h3>
    <p class="text-sm text-muted-foreground">
      Customize the sign-in screen for everyone on this instance. The dark
      overlay stays in place for readability.
    </p>
  </div>
  {#if loading}
    <p class="text-sm text-muted-foreground">Loading login background…</p>
  {:else if loadFailed}
    <p role="alert" class="text-sm text-destructive">
      Could not load login background settings.
    </p>
    <Button variant="outline" onclick={load}>Retry</Button>
  {:else}
    <div class="space-y-2">
      <Label for="login-background-mode">Background source</Label>
      <Select.Root
        name="login-background-mode"
        type="single"
        value={mode}
        onValueChange={(value) => (mode = value as LoginBackgroundMode)}
        disabled={busy}
      >
        <Select.Trigger
          id="login-background-mode"
          size="sm"
          class="w-full text-foreground sm:w-72"
        >
          {backgroundSourceLabels[mode]}
        </Select.Trigger>
        <Select.Content>
          {#each Object.entries(backgroundSourceLabels) as [value, label] (value)}
            <Select.Item {value} {label}>
              {label}
            </Select.Item>
          {/each}
        </Select.Content>
      </Select.Root>
    </div>
    {#if mode === "upload"}
      <div class="space-y-2">
        <Label for="login-background-upload"
          >{imageUrl ? "Replace image" : "Upload image"}</Label
        >
        <Input
          id="login-background-upload"
          type="file"
          accept="image/jpeg,image/png,image/webp"
          disabled={busy}
          onchange={upload}
        />
        <p class="text-xs text-muted-foreground">
          JPEG, PNG, or WebP, up to 10 MB and 25 million pixels. Uploads and
          removals are stored immediately. Save below to apply the selected
          source.
        </p>
        {#if imageUrl}<Button variant="outline" disabled={busy} onclick={remove}
            >Remove uploaded image</Button
          >{/if}
      </div>
    {:else if mode === "urls"}
      <div class="space-y-2">
        <Label for="login-background-urls">Image URLs (one per line)</Label>
        <textarea
          id="login-background-urls"
          bind:value={urlsText}
          disabled={busy}
          rows="5"
          class="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
          placeholder="https://example.com/background.jpg"></textarea>
        <p class="text-xs text-muted-foreground">
          Up to 50 direct image URLs. One image stays static; multiple images
          rotate in the listed order. These replace the default rotation. Use
          HTTPS URLs when Reclaimerr uses HTTPS. Visitors load these images
          directly from their hosts.
        </p>
      </div>
    {/if}
    {#if mode === "default" || (mode === "urls" && urls.length > 1)}
      <div class="max-w-xs space-y-2">
        <Label for="login-background-interval"
          >Rotation interval (seconds)</Label
        >
        <Input
          id="login-background-interval"
          type="number"
          min="5"
          max="300"
          step="1"
          bind:value={interval}
          disabled={busy}
        />
      </div>
    {/if}
    {#if previews.length}
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {#each previews as url, i (url)}
          <div class="overflow-hidden rounded-md border bg-background">
            {#if failedUrls.includes(url)}
              <p
                class="flex aspect-video items-center justify-center p-3 text-sm text-muted-foreground"
              >
                Image {i + 1} could not be loaded
              </p>
            {:else}
              <img
                src={url}
                alt={"Login background preview " + (i + 1)}
                referrerpolicy="no-referrer"
                loading="lazy"
                class="aspect-video w-full object-cover"
                onerror={() => (failedUrls = [...failedUrls, url])}
              />
            {/if}
          </div>
        {/each}
      </div>
    {/if}
    <p class="text-xs text-muted-foreground">
      Unavailable custom images are skipped. Default backgrounds appear if none
      can load.
    </p>
    {#if error}<p role="alert" class="text-sm text-destructive">{error}</p>{/if}
    <Button onclick={save} disabled={busy}
      >{busy ? "Working…" : "Save login background"}</Button
    >
  {/if}
</section>
