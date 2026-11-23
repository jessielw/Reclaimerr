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
  import type { GeneralSettings, OIDCSettings } from "$lib/types/shared";

  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  const defaultSettings: OIDCSettings = {
    enabled: false,
    allow_local_login: true,
    allow_media_login: true,
    allow_sso_login: true,
    media_login_configured: false,
    local_recovery_active: false,
    sso_tab_label: "",
    sso_button_label: "",
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
  let loadFailed = $state(false);
  let testing = $state(false);
  let oidcSettings = $state<OIDCSettings>({ ...defaultSettings });
  let clientSecret = $state("");
  let callbackPreview = $state("");
  let applicationUrl = $state("");

  const ssoConfigured = $derived(
    oidcSettings.enabled &&
      Boolean(oidcSettings.issuer_url.trim()) &&
      Boolean(oidcSettings.client_id.trim()) &&
      Boolean(clientSecret.trim() || oidcSettings.client_secret_configured),
  );
  const hasUsableLogin = $derived(
    oidcSettings.allow_local_login ||
      (oidcSettings.allow_media_login && oidcSettings.media_login_configured) ||
      (oidcSettings.allow_sso_login && ssoConfigured),
  );
  const restoreLoginDefaults = () => {
    oidcSettings.allow_local_login = true;
    oidcSettings.allow_media_login = true;
    oidcSettings.allow_sso_login = true;
  };

  const updateCallbackPreview = () => {
    if (typeof window === "undefined") {
      return;
    }
    const baseUrl = applicationUrl.trim() || window.location.origin;
    callbackPreview = `${baseUrl.replace(/\/+$/, "")}/api/auth/oidc/callback`;
  };

  const buildPayload = () => ({
    enabled: oidcSettings.enabled,
    allow_local_login: oidcSettings.allow_local_login,
    allow_media_login: oidcSettings.allow_media_login,
    allow_sso_login: oidcSettings.allow_sso_login,
    sso_tab_label: oidcSettings.sso_tab_label,
    sso_button_label: oidcSettings.sso_button_label,
    issuer_url: oidcSettings.issuer_url,
    client_id: oidcSettings.client_id,
    scopes: oidcSettings.scopes,
    email_claim: oidcSettings.email_claim,
    token_endpoint_auth_method: oidcSettings.token_endpoint_auth_method,
    redirect_uri_override: oidcSettings.redirect_uri_override,
    client_secret: clientSecret.trim() ? clientSecret.trim() : null,
  });

  const loadSettings = async () => {
    loading = true;
    loadFailed = false;
    try {
      const response = await get_api<OIDCSettings>("/api/settings/oidc");
      oidcSettings = {
        ...defaultSettings,
        ...response,
      };
      clientSecret = "";
      updateCallbackPreview();
    } catch (error) {
      loadFailed = true;
      toast.error(
        `Failed to load authentication settings: ${error instanceof Error ? error.message : String(error)}`,
        { duration: 10000 },
      );
    } finally {
      loading = false;
    }
  };

  const saveSettings = async () => {
    if (!hasUsableLogin || loadFailed) return;
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

  const loadGeneralSettings = async () => {
    try {
      const settings = await get_api<GeneralSettings>("/api/settings/general");
      applicationUrl = settings.application_url ?? "";
      updateCallbackPreview();
    } catch (error) {
      console.warn(
        `Failed to load general settings for callback preview: ${error instanceof Error ? error.message : String(error)}`,
      );
      updateCallbackPreview();
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
    updateCallbackPreview();
    await Promise.all([loadSettings(), loadGeneralSettings()]);
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
      Choose the allowed login methods and configure optional OpenID Connect
      single sign on.
    </p>
  </div>

  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner class="w-12 h-12 text-primary" />
    </div>
  {:else if loadFailed}
    <p class="text-sm text-destructive">
      Authentication settings could not be loaded.
    </p>
    <Button onclick={loadSettings}>Retry</Button>
  {:else}
    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm space-y-4">
      <div class="flex items-center justify-between gap-4">
        <div>
          <h3 class="font-semibold text-foreground">Login Methods</h3>
          <p class="text-sm text-muted-foreground">
            Choose what appears on the login page. Disabled methods also reject
            new sign-ins.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onclick={restoreLoginDefaults}
          disabled={saving}
        >
          Restore defaults
        </Button>
      </div>
      <div class="flex items-center justify-between gap-4">
        <div>
          <Label for="allowLocalLogin">Local login</Label>
          <p class="text-sm text-muted-foreground">
            Reclaimerr username or email and password.
          </p>
        </div>
        <Switch
          id="allowLocalLogin"
          bind:checked={oidcSettings.allow_local_login}
          disabled={saving}
        />
      </div>
      <div class="flex items-center justify-between gap-4">
        <div>
          <Label for="allowMediaLogin">Media login</Label>
          <p class="text-sm text-muted-foreground">
            Plex, Jellyfin, or Emby accounts.
            {#if !oidcSettings.media_login_configured}Configure an enabled media
              server first.{/if}
          </p>
        </div>
        <Switch
          id="allowMediaLogin"
          bind:checked={oidcSettings.allow_media_login}
          disabled={saving}
        />
      </div>
      <div class="flex items-center justify-between gap-4">
        <div>
          <Label for="allowSsoLogin">SSO login</Label>
          <p class="text-sm text-muted-foreground">
            OpenID Connect provider.
            {#if !ssoConfigured}Configure and enable the provider below first.{/if}
          </p>
        </div>
        <Switch
          id="allowSsoLogin"
          bind:checked={oidcSettings.allow_sso_login}
          disabled={saving}
        />
      </div>
      {#if !hasUsableLogin}
        <p role="alert" class="text-sm text-destructive">
          Keep at least one configured login method enabled before saving.
        </p>
      {/if}
      {#if !oidcSettings.allow_local_login}
        <p class="text-sm text-muted-foreground">
          Test that your admin account can sign in with an allowed method before
          signing out.
        </p>
      {/if}
      {#if oidcSettings.local_recovery_active}
        <p
          role="status"
          class="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm"
        >
          Local login recovery is active. Remove AUTH_FORCE_LOCAL_LOGIN and
          restart after saving a working login method.
        </p>
      {/if}
      <details class="text-sm text-muted-foreground">
        <summary class="font-medium text-foreground">
          Recover access outside the app
        </summary>
        <p class="mt-2">
          Set <code>AUTH_FORCE_LOCAL_LOGIN=true</code> in your environment or .env
          file and restart Reclaimerr. Sign in with an existing local admin username
          and password, restore the login settings, then remove the variable and restart.
          This does not reset passwords. Existing sessions remain signed in when methods
          change.
        </p>
      </details>
      <p class="text-xs text-muted-foreground">
        Use Save below to apply login methods and provider settings together.
      </p>
    </div>

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
              class="w-full text-foreground"
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
            <span class="text-sm text-foreground">
              Redirect URI Override (optional)
            </span>
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

      <div class="border-t pt-4 space-y-4">
        <div>
          <h4 class="font-semibold text-foreground">SSO Login Labels</h4>
          <p class="text-sm text-muted-foreground">
            Customize the login page text. Leave either field blank to use its
            default.
          </p>
        </div>
        <div class="grid gap-4 md:grid-cols-2">
          <div>
            <Label for="ssoTabLabel" class="mb-2">
              SSO tab text (optional)
            </Label>
            <Input
              id="ssoTabLabel"
              type="text"
              maxlength={16}
              placeholder="SSO"
              aria-describedby="ssoTabLabelHelp"
              bind:value={oidcSettings.sso_tab_label}
            />
            <p id="ssoTabLabelHelp" class="text-xs text-muted-foreground mt-1">
              Up to 16 characters. Keep it short to fit beside Local and Media.
            </p>
          </div>
          <div>
            <Label for="ssoButtonLabel" class="mb-2">
              SSO button text (optional)
            </Label>
            <Input
              id="ssoButtonLabel"
              type="text"
              maxlength={64}
              placeholder="Sign In with SSO"
              aria-describedby="ssoButtonLabelHelp"
              bind:value={oidcSettings.sso_button_label}
            />
            <p
              id="ssoButtonLabelHelp"
              class="text-xs text-muted-foreground mt-1"
            >
              Up to 64 characters, for example "Sign in with Authentik".
            </p>
          </div>
        </div>
        <div class="rounded-md border bg-background p-3 space-y-2">
          <p class="text-xs text-muted-foreground">Preview</p>
          <div class="flex items-center gap-2 text-sm">
            <span class="text-muted-foreground">Local</span>
            <span class="text-muted-foreground">Media</span>
            <span
              class="rounded-sm bg-primary px-3 py-2 text-primary-foreground"
            >
              {oidcSettings.sso_tab_label.trim() || "SSO"}
            </span>
          </div>
          <div
            class="rounded-md bg-primary px-4 py-2 text-center text-sm font-medium text-primary-foreground break-words"
          >
            {oidcSettings.sso_button_label.trim() || "Sign In with SSO"}
          </div>
        </div>
      </div>

      <div class="flex justify-end gap-3 pt-2">
        <TestButton
          onclick={testConnection}
          status={testing ? "loading" : undefined}
          disabled={testing || saving}
        >
          Test Discovery
        </TestButton>
        <Button
          onclick={saveSettings}
          disabled={saving || testing || !hasUsableLogin}
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
