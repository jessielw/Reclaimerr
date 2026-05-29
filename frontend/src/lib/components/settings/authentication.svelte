<script lang="ts">
  import { onMount } from "svelte";
  import type { Component } from "svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import TestButton from "$lib/components/test-button.svelte";
  import Save from "@lucide/svelte/icons/save";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import { get_api, post_api, put_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import type { OIDCSettings } from "$lib/types/shared";

  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  const defaultSettings: OIDCSettings = {
    enabled: false,
    issuer_url: "",
    client_id: "",
    scopes: "openid profile email",
    email_claim: "email",
    token_endpoint_auth_method: "client_secret_basic",
    redirect_uri_override: null,
    client_secret_configured: false,
  };

  let loading = $state(true);
  let saving = $state(false);
  let testing = $state(false);
  let oidcSettings = $state<OIDCSettings>({ ...defaultSettings });
  let clientSecret = $state("");
  let callbackPreview = $state("");

  const buildPayload = () => ({
    enabled: oidcSettings.enabled,
    issuer_url: oidcSettings.issuer_url,
    client_id: oidcSettings.client_id,
    scopes: oidcSettings.scopes,
    email_claim: oidcSettings.email_claim,
    token_endpoint_auth_method: oidcSettings.token_endpoint_auth_method,
    redirect_uri_override: oidcSettings.redirect_uri_override,
    client_secret: clientSecret.trim() ? clientSecret.trim() : null,
  });

  const loadSettings = async () => {
    try {
      const response = await get_api<OIDCSettings>("/api/settings/oidc");
      oidcSettings = {
        ...defaultSettings,
        ...response,
      };
      clientSecret = "";
    } catch (error) {
      toast.error(
        `Failed to load authentication settings: ${error instanceof Error ? error.message : String(error)}`,
        { duration: 10000 },
      );
    } finally {
      loading = false;
    }
  };

  const saveSettings = async () => {
    saving = true;
    try {
      const response = await put_api<OIDCSettings>(
        "/api/settings/oidc",
        buildPayload(),
      );
      oidcSettings = {
        ...defaultSettings,
        ...response,
      };
      clientSecret = "";
      toast.success("Authentication settings saved");
    } catch (error) {
      toast.error(
        `Failed to save authentication settings: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      saving = false;
    }
  };

  const testConnection = async () => {
    testing = true;
    try {
      await post_api("/api/settings/oidc/test", buildPayload());
      toast.success("OIDC discovery validation succeeded");
    } catch (error) {
      toast.error(
        `OIDC discovery validation failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      testing = false;
    }
  };

  onMount(async () => {
    if (typeof window !== "undefined") {
      callbackPreview = `${window.location.origin}/api/auth/oidc/callback`;
    }
    await loadSettings();
  });
</script>

<div class="space-y-6">
  <div>
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {#if svgIcon}
        {@const Icon = svgIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      Authentication
    </h2>
    <p class="text-sm text-muted-foreground mt-1">
      Configure optional OpenID Connect single sign on. Local username/password
      login remains available as a fallback.
    </p>
  </div>

  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner class="w-12 h-12 text-primary" />
    </div>
  {:else}
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm space-y-4">
      <div class="flex items-center justify-between">
        <div>
          <h3 class="font-semibold text-foreground">OIDC Provider</h3>
          <p class="text-sm text-muted-foreground">
            Enable sign in with Authelia or another OpenID Connect provider.
          </p>
        </div>
        <Switch id="oidcEnabled" bind:checked={oidcSettings.enabled} />
      </div>

      <div class="grid gap-4 md:grid-cols-2">
        <div class="md:col-span-2">
          <Label for="oidcIssuerUrl" class="mb-2">
            <span class="text-sm text-foreground">Issuer URL</span>
          </Label>
          <Input
            id="oidcIssuerUrl"
            type="text"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            placeholder="https://auth.example.com/application/o/reclaimerr/"
            bind:value={oidcSettings.issuer_url}
          />
        </div>

        <div>
          <Label for="oidcClientId" class="mb-2">
            <span class="text-sm text-foreground">Client ID</span>
          </Label>
          <Input
            id="oidcClientId"
            type="text"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            bind:value={oidcSettings.client_id}
          />
        </div>

        <div>
          <Label for="oidcClientSecret" class="mb-2">
            <span class="text-sm text-foreground">Client Secret</span>
          </Label>
          <Input
            id="oidcClientSecret"
            type="password"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            placeholder={oidcSettings.client_secret_configured
              ? "Saved (enter to replace)"
              : "Required when enabling OIDC"}
            bind:value={clientSecret}
          />
          {#if oidcSettings.client_secret_configured}
            <p class="text-xs text-muted-foreground mt-1">
              A secret is already saved. Leave blank to keep it unchanged.
            </p>
          {/if}
        </div>

        <div>
          <Label for="oidcScopes" class="mb-2">
            <span class="text-sm text-foreground">Scopes</span>
          </Label>
          <Input
            id="oidcScopes"
            type="text"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            placeholder="openid profile email"
            bind:value={oidcSettings.scopes}
          />
        </div>

        <div>
          <Label for="oidcEmailClaim" class="mb-2">
            <span class="text-sm text-foreground">Email Claim</span>
          </Label>
          <Input
            id="oidcEmailClaim"
            type="text"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            placeholder="email"
            bind:value={oidcSettings.email_claim}
          />
        </div>

        <div class="md:col-span-2">
          <Label for="oidcTokenAuthMethod" class="mb-2">
            <span class="text-sm text-foreground">
              Token Endpoint Auth Method
            </span>
          </Label>
          <Select.Root
            type="single"
            bind:value={oidcSettings.token_endpoint_auth_method}
          >
            <Select.Trigger
              id="oidcTokenAuthMethod"
              class="w-full cursor-pointer text-foreground"
            >
              {oidcSettings.token_endpoint_auth_method}
            </Select.Trigger>
            <Select.Content>
              <Select.Item
                value="client_secret_basic"
                label="client_secret_basic"
              >
                client_secret_basic
              </Select.Item>
              <Select.Item
                value="client_secret_post"
                label="client_secret_post"
              >
                client_secret_post
              </Select.Item>
            </Select.Content>
          </Select.Root>
          <p class="text-xs text-muted-foreground mt-1">
            Use <code>client_secret_basic</code> unless your provider requires
            <code>client_secret_post</code>.
          </p>
        </div>

        <div class="md:col-span-2">
          <Label for="oidcRedirectOverride" class="mb-2">
            <span class="text-sm text-foreground"
              >Redirect URI Override (optional)</span
            >
          </Label>
          <Input
            id="oidcRedirectOverride"
            type="text"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            placeholder={callbackPreview || "/api/auth/oidc/callback"}
            bind:value={oidcSettings.redirect_uri_override}
          />
          <p class="text-xs text-muted-foreground mt-1">
            Default callback URI:
            <code>{callbackPreview || "/api/auth/oidc/callback"}</code>
          </p>
        </div>
      </div>

      <div class="flex justify-end gap-3 pt-2">
        <TestButton
          class="cursor-pointer"
          onclick={testConnection}
          status={testing ? "loading" : undefined}
          disabled={testing || saving}
        >
          Test Discovery
        </TestButton>
        <Button
          class="cursor-pointer"
          onclick={saveSettings}
          disabled={saving || testing}
        >
          {#if saving}
            <Spinner class="size-4" />
            Saving...
          {:else}
            <Save class="size-4" />
            Save
          {/if}
        </Button>
      </div>
    </div>
  {/if}
</div>
