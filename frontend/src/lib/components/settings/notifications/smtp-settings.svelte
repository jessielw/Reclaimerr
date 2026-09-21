<script lang="ts">
  import { onMount } from "svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import TestButton from "$lib/components/test-button.svelte";
  import Save from "@lucide/svelte/icons/save";
  import Users from "@lucide/svelte/icons/users";
  import { get_api, post_api, put_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import type {
    SMTPCoverage,
    SMTPEnableAllResult,
    SMTPSettings,
  } from "$lib/types/shared";

  interface Props {
    // `destinationsCreated` tells the parent whether rows were actually added,
    // so it only reloads its list when there is something new to show and
    // never throws away a draft the user has open.
    onChanged?: (destinationsCreated: boolean) => void;
  }
  let { onChanged }: Props = $props();

  const defaultSettings: SMTPSettings = {
    enabled: false,
    host: "",
    port: 587,
    security: "starttls",
    username: "",
    from_address: "",
    from_name: "Reclaimerr",
    reply_to: null,
    password_configured: false,
    updated_at: null,
  };

  // Apprise picks these same defaults, so changing the mode should move the
  // port with it unless the admin has deliberately set something else.
  const defaultPorts: Record<SMTPSettings["security"], number> = {
    starttls: 587,
    ssl: 465,
    insecure: 25,
  };

  const securityLabels: Record<SMTPSettings["security"], string> = {
    starttls: "STARTTLS (587)",
    ssl: "SSL/TLS (465)",
    insecure: "None (25)",
  };

  let loading = $state(true);
  let saving = $state(false);
  let testing = $state(false);
  let enablingAll = $state(false);
  let settings = $state<SMTPSettings>({ ...defaultSettings });
  let password = $state("");
  let testRecipient = $state("");
  let coverage = $state<SMTPCoverage | null>(null);

  const buildPayload = () => ({
    enabled: settings.enabled,
    host: settings.host,
    port: settings.port,
    security: settings.security,
    username: settings.username,
    from_address: settings.from_address,
    from_name: settings.from_name,
    reply_to: settings.reply_to,
    password: password.trim() ? password.trim() : null,
  });

  const onSecurityChange = (value: string) => {
    const mode = value as SMTPSettings["security"];
    const wasDefaultPort = settings.port === defaultPorts[settings.security];
    settings.security = mode;
    if (wasDefaultPort) {
      settings.port = defaultPorts[mode];
    }
  };

  const loadSettings = async () => {
    try {
      const response = await get_api<SMTPSettings>("/api/settings/smtp");
      settings = { ...defaultSettings, ...response };
      password = "";
    } catch (error) {
      toast.error(
        `Failed to load SMTP settings: ${error instanceof Error ? error.message : String(error)}`,
        { duration: 10000 },
      );
    } finally {
      loading = false;
    }
  };

  const loadCoverage = async () => {
    try {
      coverage = await get_api<SMTPCoverage>("/api/settings/smtp/coverage");
    } catch (error) {
      console.warn(
        `Failed to load email coverage: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  };

  const saveSettings = async () => {
    saving = true;
    try {
      const response = await put_api<SMTPSettings>(
        "/api/settings/smtp",
        buildPayload(),
      );
      settings = { ...defaultSettings, ...response };
      password = "";
      toast.success("SMTP settings saved");
      await loadCoverage();
      // enabling SMTP is what makes an email destination offerable, so the
      // parent has to re-check rather than wait for a page reload
      onChanged?.(false);
    } catch (error) {
      toast.error(
        `Failed to save SMTP settings: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      saving = false;
    }
  };

  const sendTest = async () => {
    testing = true;
    try {
      const response = await post_api<{ message: string }>(
        "/api/settings/smtp/test",
        {
          settings: buildPayload(),
          to: testRecipient.trim() ? testRecipient.trim() : null,
        },
      );
      toast.success(response.message ?? "Test email sent");
    } catch (error) {
      toast.error(
        `Test email failed: ${error instanceof Error ? error.message : String(error)}`,
        { duration: 10000 },
      );
    } finally {
      testing = false;
    }
  };

  const enableForAll = async () => {
    enablingAll = true;
    try {
      const response = await post_api<SMTPEnableAllResult>(
        "/api/settings/smtp/enable-all",
        {},
      );
      if (response.created === 0) {
        toast.info("Every eligible user already has an email destination");
      } else {
        toast.success(
          `Email notifications enabled for ${response.created} user${response.created === 1 ? "" : "s"}`,
        );
      }
      await loadCoverage();
      onChanged?.(response.created > 0);
    } catch (error) {
      toast.error(
        `Failed to enable email notifications: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      enablingAll = false;
    }
  };

  onMount(async () => {
    await Promise.all([loadSettings(), loadCoverage()]);
  });
</script>

<div class="bg-muted/50 border rounded-lg p-4 shadow-sm space-y-4">
  <div class="flex items-center justify-between">
    <div>
      <h3 class="font-semibold text-foreground">Email server (SMTP)</h3>
      <p class="text-sm text-muted-foreground">
        Configure one mail server for the whole instance. Users can then have
        notifications sent to their account email address without building an
        Apprise URL themselves.
      </p>
    </div>
    <Switch id="smtpEnabled" bind:checked={settings.enabled} />
  </div>

  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner class="w-12 h-12 text-primary" />
    </div>
  {:else}
    <div class="grid gap-4 md:grid-cols-2">
      <div>
        <Label for="smtpHost" class="mb-2">
          <span class="text-sm text-foreground">Host</span>
        </Label>
        <Input
          id="smtpHost"
          type="text"
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
          placeholder="smtp.example.com"
          bind:value={settings.host}
        />
      </div>

      <div class="grid grid-cols-2 gap-4">
        <div>
          <Label for="smtpSecurity" class="mb-2">
            <span class="text-sm text-foreground">Encryption</span>
          </Label>
          <Select.Root
            type="single"
            value={settings.security}
            onValueChange={onSecurityChange}
          >
            <Select.Trigger
              id="smtpSecurity"
              class="w-full cursor-pointer text-foreground"
            >
              {securityLabels[settings.security]}
            </Select.Trigger>
            <Select.Content>
              <Select.Item value="starttls" label={securityLabels.starttls}>
                {securityLabels.starttls}
              </Select.Item>
              <Select.Item value="ssl" label={securityLabels.ssl}>
                {securityLabels.ssl}
              </Select.Item>
              <Select.Item value="insecure" label={securityLabels.insecure}>
                {securityLabels.insecure}
              </Select.Item>
            </Select.Content>
          </Select.Root>
        </div>

        <div>
          <Label for="smtpPort" class="mb-2">
            <span class="text-sm text-foreground">Port</span>
          </Label>
          <Input
            id="smtpPort"
            type="number"
            min="1"
            max="65535"
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
            bind:value={settings.port}
          />
        </div>
      </div>

      <div>
        <Label for="smtpUsername" class="mb-2">
          <span class="text-sm text-foreground">Username (optional)</span>
        </Label>
        <Input
          id="smtpUsername"
          type="text"
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
          placeholder="Leave blank for an unauthenticated relay"
          bind:value={settings.username}
        />
      </div>

      <div>
        <Label for="smtpPassword" class="mb-2">
          <span class="text-sm text-foreground">Password</span>
        </Label>
        <Input
          id="smtpPassword"
          type="password"
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
          placeholder={settings.password_configured
            ? "Saved (enter to replace)"
            : "Leave blank for an unauthenticated relay"}
          bind:value={password}
        />
        {#if settings.password_configured}
          <p class="text-xs text-muted-foreground mt-1">
            A password is already saved. Leave blank to keep it unchanged.
          </p>
        {/if}
      </div>

      <div>
        <Label for="smtpFromAddress" class="mb-2">
          <span class="text-sm text-foreground">From address</span>
        </Label>
        <Input
          id="smtpFromAddress"
          type="text"
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
          placeholder="reclaimerr@example.com"
          bind:value={settings.from_address}
        />
      </div>

      <div>
        <Label for="smtpFromName" class="mb-2">
          <span class="text-sm text-foreground">From name</span>
        </Label>
        <Input
          id="smtpFromName"
          type="text"
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
          placeholder="Reclaimerr"
          bind:value={settings.from_name}
        />
      </div>

      <div>
        <Label for="smtpReplyTo" class="mb-2">
          <span class="text-sm text-foreground">Reply-to (optional)</span>
        </Label>
        <Input
          id="smtpReplyTo"
          type="text"
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
          placeholder="noreply@example.com"
          bind:value={settings.reply_to}
        />
      </div>

      <div>
        <Label for="smtpTestRecipient" class="mb-2">
          <span class="text-sm text-foreground">Send test to (optional)</span>
        </Label>
        <Input
          id="smtpTestRecipient"
          type="text"
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
          placeholder="Defaults to your account email"
          bind:value={testRecipient}
        />
      </div>
    </div>

    <div class="flex justify-end gap-3 pt-2">
      <TestButton
        class="cursor-pointer"
        onclick={sendTest}
        status={testing ? "loading" : undefined}
        disabled={testing || saving}
      >
        Send Test Email
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

    {#if coverage}
      <div class="border-t pt-4 space-y-3">
        <div>
          <h4 class="text-sm font-semibold text-foreground">
            Enable for existing users
          </h4>
          <p class="text-sm text-muted-foreground">
            {coverage.with_email} of {coverage.total_users} active
            {coverage.total_users === 1 ? "user has" : "users have"} an email address,
            and {coverage.already_enabled} already
            {coverage.already_enabled === 1 ? "receives" : "receive"} email. This
            creates a destination each user can then edit or delete; it never changes
            one they already have.
          </p>
        </div>
        <div class="flex justify-end">
          <Button
            variant="outline"
            class="cursor-pointer"
            onclick={enableForAll}
            disabled={enablingAll || coverage.eligible === 0}
          >
            {#if enablingAll}
              <Spinner class="size-4" />
              Enabling...
            {:else}
              <Users class="size-4" />
              {coverage.eligible === 0
                ? "Nothing to enable"
                : `Enable for ${coverage.eligible} remaining`}
            {/if}
          </Button>
        </div>
      </div>
    {/if}
  {/if}
</div>
