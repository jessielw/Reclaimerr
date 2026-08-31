<script lang="ts">
  import type { Component } from "svelte";
  import { post_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import TestButton from "$lib/components/test-button.svelte";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import BadgeCheck from "@lucide/svelte/icons/badge-check";
  import Save from "@lucide/svelte/icons/save";
  import { toast } from "svelte-sonner";
  import type {
    MediaServerState,
    SaveServiceResponse,
    ServerKey,
  } from "./types";

  interface Props {
    open: boolean;
    serverKey: ServerKey;
    label: string;
    icon: Component | null;
    urlPlaceholder: string;
    apiKeyLabel: string;
    /** null when adding a server, the saved row when editing one. */
    existing: MediaServerState | null;
    /** true when saving this server also makes it the main server. */
    willBeMain: boolean;
    /** names already taken by other servers of this same type. */
    takenNames: string[];
    /** suggested name for a new server, unique among `takenNames`. */
    suggestedName: string;
    onSaved: (response: SaveServiceResponse) => void;
  }

  let {
    open = $bindable(),
    serverKey,
    label,
    icon,
    urlPlaceholder,
    apiKeyLabel,
    existing,
    willBeMain,
    takenNames,
    suggestedName,
    onSaved,
  }: Props = $props();

  let name = $state("");
  let baseUrl = $state("");
  let apiKey = $state("");
  let enabled = $state(true);
  let apiKeyIsSet = $state(false);
  let testStatus = $state<"idle" | "loading" | "success" | "error">("idle");
  let testing = $state(false);
  let saving = $state(false);

  const isEdit = $derived(existing !== null);
  const keyNoun = $derived(apiKeyLabel.toLowerCase().replace("api", "API"));

  // the main server is the source of library data, so it can never be left
  // disabled - the backend rejects that outright
  const effectiveEnabled = $derived(willBeMain ? true : enabled);

  const nameCollision = $derived(
    takenNames.some(
      (taken) => taken.toLowerCase() === name.trim().toLowerCase(),
    ),
  );

  const canSave = $derived(
    !saving &&
      !!name.trim() &&
      !!baseUrl.trim() &&
      !nameCollision &&
      (apiKeyIsSet || !!apiKey.trim()),
  );

  // seed the form from the target row each time the dialog opens, and clear the
  // flag on close so a stale draft never leaks into the next server's form
  let initialized = false;
  $effect(() => {
    if (open && !initialized) {
      name = existing ? existing.config.name : suggestedName;
      baseUrl = existing ? existing.config.baseUrl : "";
      enabled = existing ? existing.config.enabled : true;
      apiKeyIsSet = existing ? existing.apiKeyIsSet : false;
      apiKey = "";
      testStatus = "idle";
      initialized = true;
    } else if (!open) {
      initialized = false;
    }
  });

  const testConnection = async () => {
    testing = true;
    testStatus = "loading";
    try {
      const payload: Record<string, unknown> = {
        id: existing?.config.id ?? null,
        name: name.trim() || label,
        service_type: serverKey,
        enabled: effectiveEnabled,
        base_url: baseUrl.trim(),
      };
      // omitted key means "reuse the stored one", which the backend resolves
      // from the id above
      if (apiKey.trim()) payload.api_key = apiKey.trim();
      await post_api("/api/settings/test/service", payload);
      testStatus = "success";
    } catch (err: any) {
      testStatus = "error";
      toast.error(`Connection test failed: ${err.message}`);
    } finally {
      testing = false;
    }
  };

  const save = async () => {
    saving = true;
    let saved: SaveServiceResponse | null = null;
    try {
      saved = await post_api<SaveServiceResponse>(
        "/api/settings/save/service",
        {
          id: existing?.config.id ?? null,
          name: name.trim() || label,
          service_type: serverKey,
          enabled: effectiveEnabled,
          base_url: baseUrl.trim(),
          is_main: willBeMain,
          ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        },
      );
    } catch (err: any) {
      toast.error(`Error saving ${label} settings: ${err.message}`);
    } finally {
      // released before the close below, so the in-flight guard on
      // onOpenChange cannot bounce the dialog back open
      saving = false;
    }
    if (!saved) return;
    toast.success(saved.message);
    open = false;
    onSaved(saved);
  };
</script>

<Dialog.Root
  bind:open
  onOpenChange={(next) => {
    // never let a click-away strand an in-flight save
    if (!next && saving) open = true;
  }}
>
  <Dialog.Content class="sm:max-w-125 text-foreground">
    <Dialog.Header>
      <Dialog.Title class="flex items-center gap-2">
        {#if icon}
          {@const Icon = icon}
          <Icon class="size-5 shrink-0" aria-hidden="true" />
        {/if}
        {isEdit ? `Edit ${label} server` : `Add ${label} server`}
        {#if willBeMain}
          <span
            class="flex items-center gap-1 text-xs font-medium text-primary"
          >
            <BadgeCheck class="size-3.5" />
            Main
          </span>
        {/if}
      </Dialog.Title>
      <Dialog.Description>
        {#if willBeMain && !isEdit}
          This is your first media server, so it becomes the main server - the
          source of all library and media data.
        {:else if willBeMain}
          The main server is the source of all library and media data.
        {:else}
          A linked server contributes watch history and user data. Library
          contents always come from the main server.
        {/if}
      </Dialog.Description>
    </Dialog.Header>

    <div class="space-y-4 py-2">
      <div class="space-y-2">
        <Label for="media-server-name">Name</Label>
        <Input
          id="media-server-name"
          type="text"
          bind:value={name}
          placeholder={`${label} instance`}
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
        />
        {#if nameCollision}
          <p class="text-xs text-destructive">
            Another {label} server already uses this name. Pick a different one.
          </p>
        {:else}
          <p class="text-xs text-muted-foreground">
            Labels this server's libraries once more than one server is
            configured.
          </p>
        {/if}
      </div>

      <div class="space-y-2">
        <Label for="media-server-url">Base URL</Label>
        <Input
          id="media-server-url"
          type="url"
          bind:value={baseUrl}
          oninput={() => (testStatus = "idle")}
          placeholder={urlPlaceholder}
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
        />
      </div>

      <div class="space-y-2">
        <Label for="media-server-key">{apiKeyLabel}</Label>
        <Input
          id="media-server-key"
          type="password"
          bind:value={apiKey}
          oninput={() => (testStatus = "idle")}
          placeholder={apiKeyIsSet
            ? `Leave blank to keep existing ${keyNoun}`
            : `Enter your ${keyNoun}`}
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
        />
      </div>

      <div class="flex items-center justify-between rounded-md border p-3">
        <div class="pr-3">
          <p class="text-sm font-medium">Enabled</p>
          <p class="text-xs text-muted-foreground">
            {willBeMain
              ? "The main server is always enabled."
              : "Disabled servers are kept but never contacted."}
          </p>
        </div>
        <Switch
          class="cursor-pointer"
          checked={effectiveEnabled}
          disabled={willBeMain}
          onCheckedChange={(checked) => (enabled = checked)}
        />
      </div>

      {#if effectiveEnabled}
        <p class="text-xs text-muted-foreground">
          Saving verifies the connection first - an unreachable server is not
          saved as enabled.
        </p>
      {/if}
    </div>

    <Dialog.Footer class="gap-2">
      <TestButton
        onclick={testConnection}
        disabled={testing || saving || !baseUrl.trim()}
        status={testStatus}
        class="cursor-pointer gap-2 sm:mr-auto"
        size="sm">Test</TestButton
      >
      <Button
        variant="outline"
        size="sm"
        class="cursor-pointer"
        disabled={saving}
        onclick={() => (open = false)}>Cancel</Button
      >
      <Button
        size="sm"
        class="cursor-pointer gap-2"
        disabled={!canSave}
        onclick={save}
      >
        {#if saving}
          <Spinner class="size-4" />
          Saving...
        {:else}
          <Save class="size-4" />
          {isEdit ? "Save" : "Add server"}
        {/if}
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
