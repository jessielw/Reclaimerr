<script lang="ts">
  import { onMount } from "svelte";
  import type { Component } from "svelte";
  import { delete_api, get_api, post_api, put_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import {
    MediaType,
    type ApiTokenScope,
    type ApiTokenCreated,
    type ApiTokenInfo,
    type LifecycleEventType,
    type LifecycleWebhookEndpoint,
    type WebhookDeliveryInfo,
  } from "$lib/types/shared";
  import Copy from "@lucide/svelte/icons/copy";
  import Plus from "@lucide/svelte/icons/plus";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import { toast } from "svelte-sonner";

  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  const eventOptions: { value: LifecycleEventType; label: string }[] = [
    { value: "candidate.scheduled", label: "Scheduled" },
    { value: "candidate.canceled", label: "Canceled" },
    { value: "candidate.postponed", label: "Postponed" },
    { value: "candidate.timer_reset", label: "Timer reset" },
    { value: "candidate.protected", label: "Protected" },
    { value: "candidate.deleted", label: "Deleted" },
    { value: "candidate.moved", label: "Moved" },
    { value: "protection.created", label: "Protection created" },
    { value: "protection.removed", label: "Protection removed" },
  ];

  const scopeOptions: {
    value: ApiTokenScope;
    label: string;
    description: string;
  }[] = [
    {
      value: "system:read",
      label: "System",
      description: "Version and sync health",
    },
    {
      value: "media:read",
      label: "Media",
      description: "Movies and series catalog",
    },
    {
      value: "candidates:read",
      label: "Candidates",
      description: "Candidate status",
    },
    {
      value: "candidates:manage",
      label: "Manage candidates",
      description: "Cancel, postpone and reset",
    },
    {
      value: "events:read",
      label: "Events",
      description: "Lifecycle event feed",
    },
    {
      value: "protections:read",
      label: "Protections",
      description: "Protection status",
    },
    {
      value: "protections:manage",
      label: "Manage protections",
      description: "Create and remove protections",
    },
    {
      value: "tasks:read",
      label: "Tasks",
      description: "Schedules and run history",
    },
    {
      value: "tasks:run",
      label: "Run tasks",
      description: "Trigger enabled tasks",
    },
  ];

  const readOnlyScopes: ApiTokenScope[] = [
    "system:read",
    "media:read",
    "candidates:read",
    "events:read",
    "protections:read",
    "tasks:read",
  ];

  let loading = $state(true);
  let tokenName = $state("");
  let tokenScopes = $state<ApiTokenScope[]>([...readOnlyScopes]);
  let tokenExpiresAt = $state("");
  let createdToken = $state<ApiTokenCreated | null>(null);
  let tokens = $state<ApiTokenInfo[]>([]);
  let webhooks = $state<LifecycleWebhookEndpoint[]>([]);
  let deliveries = $state<WebhookDeliveryInfo[]>([]);

  const formatDate = (value: string | null) =>
    value ? new Date(value).toLocaleString() : "Never";

  const newWebhook = (): LifecycleWebhookEndpoint => ({
    id: 0,
    name: "Candidate lifecycle webhook",
    enabled: true,
    method: "POST",
    url_template: "",
    event_types: ["candidate.scheduled"],
    media_types: [MediaType.Movie, MediaType.Series],
    path_mode: "original",
    body_template: null,
    timeout_seconds: 15,
    auth_username: null,
    auth_password: null,
    auth_password_is_set: false,
    headers: [],
    created_at: "",
    updated_at: "",
  });

  const load = async () => {
    loading = true;
    try {
      [tokens, webhooks, deliveries] = await Promise.all([
        get_api<ApiTokenInfo[]>("/api/settings/integrations/api-tokens"),
        get_api<LifecycleWebhookEndpoint[]>(
          "/api/settings/integrations/webhooks",
        ),
        get_api<WebhookDeliveryInfo[]>(
          "/api/settings/integrations/webhook-deliveries?limit=50",
        ),
      ]);
    } catch (error) {
      toast.error(
        `Failed to load integrations: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      loading = false;
    }
  };

  const createToken = async () => {
    if (!tokenName.trim()) {
      toast.error("Enter a token name");
      return;
    }
    if (tokenScopes.length === 0) {
      toast.error("Select at least one API scope");
      return;
    }
    try {
      createdToken = await post_api<ApiTokenCreated>(
        "/api/settings/integrations/api-tokens",
        {
          name: tokenName.trim(),
          scopes: tokenScopes,
          expires_at: tokenExpiresAt
            ? new Date(tokenExpiresAt).toISOString()
            : null,
        },
      );
      tokens = [createdToken, ...tokens];
      tokenName = "";
      tokenExpiresAt = "";
      toast.success("API token created");
    } catch (error) {
      toast.error(
        `Failed to create API token: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  };

  const toggleScope = (scope: ApiTokenScope) => {
    tokenScopes = tokenScopes.includes(scope)
      ? tokenScopes.filter((value) => value !== scope)
      : [...tokenScopes, scope];
  };

  const copyToken = async () => {
    if (!createdToken) return;
    await navigator.clipboard.writeText(createdToken.token);
    toast.success("API token copied");
  };

  const revokeToken = async (id: number) => {
    try {
      const updated = await delete_api<ApiTokenInfo>(
        `/api/settings/integrations/api-tokens/${id}`,
      );
      tokens = tokens.map((token) => (token.id === id ? updated : token));
      toast.success("API token revoked");
    } catch (error) {
      toast.error(
        `Failed to revoke token: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  };

  const addWebhook = () => {
    webhooks = [...webhooks, newWebhook()];
  };

  const toggleEvent = (
    webhook: LifecycleWebhookEndpoint,
    event: LifecycleEventType,
  ) => {
    webhook.event_types = webhook.event_types.includes(event)
      ? webhook.event_types.filter((value) => value !== event)
      : [...webhook.event_types, event];
    webhooks = [...webhooks];
  };

  const toggleMediaType = (
    webhook: LifecycleWebhookEndpoint,
    mediaType: MediaType,
  ) => {
    webhook.media_types = webhook.media_types.includes(mediaType)
      ? webhook.media_types.filter((value) => value !== mediaType)
      : [...webhook.media_types, mediaType];
    webhooks = [...webhooks];
  };

  const addHeader = (webhook: LifecycleWebhookEndpoint) => {
    webhook.headers = [...webhook.headers, { name: "", value: "" }];
    webhooks = [...webhooks];
  };

  const removeHeader = (webhook: LifecycleWebhookEndpoint, index: number) => {
    webhook.headers = webhook.headers.filter(
      (_, itemIndex) => itemIndex !== index,
    );
    webhooks = [...webhooks];
  };

  const webhookPayload = (webhook: LifecycleWebhookEndpoint) => ({
    name: webhook.name,
    enabled: webhook.enabled,
    method: webhook.method,
    url_template: webhook.url_template,
    event_types: webhook.event_types,
    media_types: webhook.media_types,
    path_mode: webhook.path_mode,
    body_template: webhook.body_template || null,
    timeout_seconds: Number(webhook.timeout_seconds),
    auth_username: webhook.auth_username || null,
    auth_password: webhook.auth_password || null,
    headers: webhook.headers.filter((header) => header.name.trim()),
  });

  const saveWebhook = async (webhook: LifecycleWebhookEndpoint) => {
    try {
      const saved = webhook.id
        ? await put_api<LifecycleWebhookEndpoint>(
            `/api/settings/integrations/webhooks/${webhook.id}`,
            webhookPayload(webhook),
          )
        : await post_api<LifecycleWebhookEndpoint>(
            "/api/settings/integrations/webhooks",
            webhookPayload(webhook),
          );
      webhooks = webhooks.map((item) =>
        item === webhook || item.id === saved.id ? saved : item,
      );
      toast.success("Webhook saved");
    } catch (error) {
      toast.error(
        `Failed to save webhook: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  };

  const testWebhook = async (webhook: LifecycleWebhookEndpoint) => {
    try {
      if (webhook.id) {
        await post_api(
          `/api/settings/integrations/webhooks/${webhook.id}/test`,
        );
      } else {
        await post_api(
          "/api/settings/integrations/webhooks/test",
          webhookPayload(webhook),
        );
      }
      toast.success("Test webhook delivered");
    } catch (error) {
      toast.error(
        `Webhook test failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  };

  const removeWebhook = async (webhook: LifecycleWebhookEndpoint) => {
    if (!webhook.id) {
      webhooks = webhooks.filter((item) => item !== webhook);
      return;
    }
    try {
      await delete_api(`/api/settings/integrations/webhooks/${webhook.id}`);
      webhooks = webhooks.filter((item) => item.id !== webhook.id);
      toast.success("Webhook removed");
    } catch (error) {
      toast.error(
        `Failed to remove webhook: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  };

  const retryDelivery = async (delivery: WebhookDeliveryInfo) => {
    try {
      const updated = await post_api<WebhookDeliveryInfo>(
        `/api/settings/integrations/webhook-deliveries/${delivery.id}/retry`,
      );
      deliveries = deliveries.map((item) =>
        item.id === updated.id ? updated : item,
      );
      toast.success("Webhook delivery queued again");
    } catch (error) {
      toast.error(
        `Failed to retry delivery: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  };

  onMount(load);
</script>

<div class="space-y-6">
  <div>
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {#if svgIcon}
        {@const Icon = svgIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      Integrations
    </h2>
    <p class="mt-1 text-sm text-muted-foreground">
      Connect external automation to candidate lifecycle actions and durable
      webhook events.
    </p>
  </div>

  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner class="size-10 text-primary" />
    </div>
  {:else}
    <section class="space-y-4 rounded-lg border bg-muted/50 p-4 shadow-sm">
      <div>
        <h3 class="font-semibold text-foreground">API Tokens</h3>
        <p class="text-sm text-muted-foreground">
          Grant only the API capabilities each integration needs. The secret is
          shown once.
        </p>
      </div>
      <div class="grid gap-3 md:grid-cols-[1fr_14rem_auto]">
        <Input placeholder="Home Assistant" bind:value={tokenName} />
        <Input type="datetime-local" bind:value={tokenExpiresAt} />
        <Button class="cursor-pointer gap-2" onclick={createToken}>
          <Plus class="size-4" /> Create
        </Button>
      </div>
      <div class="rounded-md border bg-background/50 p-3">
        <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
          <Label>Scopes</Label>
          <div class="flex gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              class="cursor-pointer"
              onclick={() => (tokenScopes = [...readOnlyScopes])}
              >Read only</Button
            >
            <Button
              type="button"
              variant="outline"
              size="sm"
              class="cursor-pointer"
              onclick={() =>
                (tokenScopes = scopeOptions.map((option) => option.value))}
              >Full automation</Button
            >
          </div>
        </div>
        <div class="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {#each scopeOptions as option}
            <label
              class="flex cursor-pointer items-start gap-2 rounded-md p-2 hover:bg-muted"
            >
              <input
                class="mt-1"
                type="checkbox"
                checked={tokenScopes.includes(option.value)}
                onchange={() => toggleScope(option.value)}
              />
              <span>
                <span class="block text-sm font-medium">{option.label}</span>
                <span class="block text-xs text-muted-foreground"
                  >{option.description}</span
                >
              </span>
            </label>
          {/each}
        </div>
      </div>

      {#if createdToken}
        <div class="rounded-md border border-amber-500/50 bg-amber-500/10 p-3">
          <p class="text-sm font-medium">
            Copy this token now; it will not be shown again.
          </p>
          <div class="mt-2 flex gap-2">
            <Input readonly value={createdToken.token} class="font-mono" />
            <Button
              size="icon"
              class="cursor-pointer"
              onclick={copyToken}
              aria-label="Copy token"
            >
              <Copy class="size-4" />
            </Button>
          </div>
        </div>
      {/if}

      <div class="space-y-2">
        {#each tokens as token}
          <div
            class="flex flex-wrap items-center justify-between gap-3 rounded-md border bg-background/50 p-3"
          >
            <div>
              <div class="font-medium text-foreground">{token.name}</div>
              <div class="text-xs text-muted-foreground">
                rcl_{token.token_prefix}_… · {token.scopes.join(", ")} · Last used
                {formatDate(token.last_used_at)}
              </div>
            </div>
            {#if token.revoked_at}
              <span class="text-xs font-medium text-destructive">Revoked</span>
            {:else}
              <Button
                variant="destructive"
                size="sm"
                class="cursor-pointer"
                onclick={() => revokeToken(token.id)}
              >
                Revoke
              </Button>
            {/if}
          </div>
        {/each}
      </div>
    </section>

    <section class="space-y-4 rounded-lg border bg-muted/50 p-4 shadow-sm">
      <div class="flex items-center justify-between gap-3">
        <div>
          <h3 class="font-semibold text-foreground">Lifecycle Webhooks</h3>
          <p class="text-sm text-muted-foreground">
            Failed deliveries retry automatically and survive server restarts.
          </p>
        </div>
        <Button size="sm" class="cursor-pointer gap-2" onclick={addWebhook}>
          <Plus class="size-4" /> Add webhook
        </Button>
      </div>

      {#each webhooks as webhook}
        <div class="space-y-3 rounded-lg border bg-background/50 p-3">
          <div class="flex items-center justify-between gap-3">
            <div class="flex items-center gap-2">
              <Switch bind:checked={webhook.enabled} />
              <span class="font-medium">{webhook.name}</span>
            </div>
            <div class="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                class="cursor-pointer"
                onclick={() => testWebhook(webhook)}>Test</Button
              >
              <Button
                size="sm"
                class="cursor-pointer"
                onclick={() => saveWebhook(webhook)}>Save</Button
              >
              <Button
                variant="destructive"
                size="icon-sm"
                class="cursor-pointer"
                onclick={() => removeWebhook(webhook)}
                aria-label="Remove webhook"
              >
                <Trash2 class="size-4" />
              </Button>
            </div>
          </div>
          <div class="grid gap-3 md:grid-cols-[1fr_7rem]">
            <div>
              <Label class="mb-1">Name</Label><Input
                bind:value={webhook.name}
              />
            </div>
            <div>
              <Label class="mb-1">Method</Label>
              <select
                class="h-9 w-full rounded-md border bg-background px-3 text-sm"
                bind:value={webhook.method}
              >
                <option value="POST">POST</option><option value="GET"
                  >GET</option
                >
              </select>
            </div>
          </div>
          <div>
            <Label class="mb-1">URL template</Label><Input
              class="font-mono"
              placeholder="https://automation.example/webhook"
              bind:value={webhook.url_template}
            />
          </div>
          <div class="grid gap-3 md:grid-cols-2">
            <div>
              <Label class="mb-1">Events</Label>
              <div class="flex flex-wrap gap-x-4 gap-y-2 text-sm">
                {#each eventOptions as option}
                  <label class="flex cursor-pointer items-center gap-2">
                    <input
                      type="checkbox"
                      checked={webhook.event_types.includes(option.value)}
                      onchange={() => toggleEvent(webhook, option.value)}
                    />
                    {option.label}
                  </label>
                {/each}
              </div>
            </div>
            <div>
              <Label class="mb-1">Media types</Label>
              <div class="flex gap-4 text-sm">
                <label class="flex cursor-pointer items-center gap-2"
                  ><input
                    type="checkbox"
                    checked={webhook.media_types.includes(MediaType.Movie)}
                    onchange={() => toggleMediaType(webhook, MediaType.Movie)}
                  /> Movies</label
                >
                <label class="flex cursor-pointer items-center gap-2"
                  ><input
                    type="checkbox"
                    checked={webhook.media_types.includes(MediaType.Series)}
                    onchange={() => toggleMediaType(webhook, MediaType.Series)}
                  /> Series</label
                >
              </div>
            </div>
          </div>
          <div class="grid gap-3 md:grid-cols-2">
            <div>
              <Label class="mb-1">Basic auth username</Label><Input
                placeholder="Optional"
                bind:value={webhook.auth_username}
              />
            </div>
            <div>
              <Label class="mb-1">Basic auth password</Label><Input
                type="password"
                placeholder={webhook.auth_password_is_set
                  ? "Saved (enter to replace)"
                  : "Optional"}
                bind:value={webhook.auth_password}
              />
            </div>
          </div>
          <div class="space-y-2">
            <div class="flex items-center justify-between">
              <Label>Headers</Label>
              <Button
                variant="outline"
                size="sm"
                class="cursor-pointer"
                onclick={() => addHeader(webhook)}>Add header</Button
              >
            </div>
            {#each webhook.headers as header, headerIndex}
              <div class="grid gap-2 md:grid-cols-[1fr_1fr_auto]">
                <Input placeholder="Header name" bind:value={header.name} />
                <Input
                  type="password"
                  placeholder="Header value"
                  bind:value={header.value}
                />
                <Button
                  variant="destructive"
                  size="icon-sm"
                  class="cursor-pointer"
                  onclick={() => removeHeader(webhook, headerIndex)}
                  aria-label="Remove header"><Trash2 class="size-4" /></Button
                >
              </div>
            {/each}
          </div>
          {#if webhook.method === "POST"}
            <div>
              <Label class="mb-1">Body template</Label><textarea
                class="min-h-20 w-full rounded-md border bg-background px-3 py-2 font-mono text-sm"
                placeholder="Leave empty to send the full JSON event"
                bind:value={webhook.body_template}
              ></textarea>
            </div>
          {/if}
        </div>
      {/each}
    </section>

    <section class="space-y-3 rounded-lg border bg-muted/50 p-4 shadow-sm">
      <div class="flex items-center justify-between">
        <div>
          <h3 class="font-semibold">Recent Deliveries</h3>
          <p class="text-sm text-muted-foreground">
            The latest durable webhook attempts.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          class="cursor-pointer"
          onclick={load}>Refresh</Button
        >
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead class="text-xs text-muted-foreground"
            ><tr
              ><th class="py-2">Endpoint</th><th>Event</th><th>Status</th><th
                >Attempts</th
              ><th>Created</th><th></th></tr
            ></thead
          >
          <tbody>
            {#each deliveries as delivery}
              <tr class="border-t"
                ><td class="py-2">{delivery.endpoint_name}</td><td
                  >{delivery.event_type}</td
                ><td>{delivery.status}</td><td>{delivery.attempts}</td><td
                  >{formatDate(delivery.created_at)}</td
                ><td class="text-right"
                  >{#if delivery.status === "failed"}<Button
                      variant="outline"
                      size="icon-sm"
                      class="cursor-pointer"
                      onclick={() => retryDelivery(delivery)}
                      aria-label="Retry delivery"
                      ><RotateCcw class="size-4" /></Button
                    >{/if}</td
                ></tr
              >
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}
</div>
