<script lang="ts">
  import type { Component } from "svelte";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import { Input } from "$lib/components/ui/input/index.js";

  interface Props {
    tabLabel: string;
    tabIcon: Component | null;
    enabled: boolean;
    name?: string;
    lockedName?: string;
    baseUrl: string;
    apiKey: string;
    apiKeyIsSet?: boolean;
    apiKeyLabel?: string;
    baseUrlPlaceholder?: string;
    hideBaseUrl?: boolean;
    fixedBaseUrl?: string;
    disableToggle?: boolean;
    extraSettings?: Record<string, any>;
    onchange?: (event: CustomEvent) => void;
  }

  let {
    tabLabel,
    tabIcon,
    enabled,
    name = "",
    lockedName,
    baseUrl,
    apiKey,
    apiKeyIsSet = false,
    apiKeyLabel = "API Key",
    baseUrlPlaceholder,
    hideBaseUrl = false,
    fixedBaseUrl,
    disableToggle = false,
    extraSettings = {},
    onchange,
  }: Props = $props();

  function dispatchChange(field: string, value: any) {
    if (onchange) {
      onchange(new CustomEvent("change", { detail: { field, value } }));
    }
  }
</script>

<div class="space-y-6">
  <div class="flex items-center justify-between mb-6">
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {#if tabIcon}
        {@const Icon = tabIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      <span class="align-middle">{tabLabel}</span>
    </h2>
    <label class="flex items-center gap-2 cursor-pointer">
      <span class="text-sm text-foreground">Enable</span>
      <Switch
        class="cursor-pointer"
        checked={disableToggle ? true : enabled}
        disabled={disableToggle}
        onCheckedChange={(checked) => dispatchChange("enabled", checked)}
      />
    </label>
  </div>

  <div>
    <label for="name" class="block text-sm font-medium text-foreground mb-2"
      >Name</label
    >
    <Input
      type="text"
      name="name"
      value={name || lockedName || ""}
      oninput={(e) => dispatchChange("name", e.currentTarget.value)}
      placeholder={`${tabLabel} instance`}
      class="input-hover-el text-foreground placeholder:text-muted-foreground"
      disabled={lockedName != null}
    />
  </div>

  {#if hideBaseUrl}
    {#if fixedBaseUrl}
      <div class="rounded-md border border-border bg-muted/30 px-3 py-2">
        <p
          class="text-xs font-medium uppercase tracking-wide text-muted-foreground"
        >
          API Endpoint
        </p>
        <p class="mt-1 text-sm text-foreground">{fixedBaseUrl}</p>
      </div>
    {/if}
  {:else}
    <div>
      <label
        for="baseUrl"
        class="block text-sm font-medium text-foreground mb-2">Base URL</label
      >
      <Input
        type="url"
        name="baseUrl"
        value={baseUrl}
        oninput={(e) => dispatchChange("baseUrl", e.currentTarget.value)}
        placeholder={baseUrlPlaceholder || "http://localhost:8096"}
        class="input-hover-el text-foreground placeholder:text-muted-foreground"
      />
      <p class="mt-1 text-xs text-muted-foreground">
        The URL where your {tabLabel} instance is running
      </p>
    </div>
  {/if}

  <div>
    <label for="apiKey" class="block text-sm font-medium text-foreground mb-2"
      >{apiKeyLabel}</label
    >
    <Input
      type="password"
      name="apiKey"
      value={apiKey}
      oninput={(e) => dispatchChange("apiKey", e.currentTarget.value)}
      placeholder={apiKeyIsSet
        ? `Leave blank to keep existing ${apiKeyLabel.toLowerCase().replace("api", "API")}`
        : `Enter your ${apiKeyLabel.toLowerCase().replace("api", "API")}`}
      class="input-hover-el text-foreground placeholder:text-muted-foreground"
    />
    <p class="mt-1 text-xs text-muted-foreground">
      Your {tabLabel}
      {apiKeyLabel.toLowerCase().replace("api", "API")} for authentication
    </p>
  </div>

  <!-- extra settings -->
  {#if Object.keys(extraSettings).length > 0}
    <!-- timeout -->
    {#if "timeout" in extraSettings}
      <div>
        <div>
          <label
            for="timetout"
            class="block text-sm font-medium text-foreground mb-2"
            >Timeout</label
          >
          <Input
            type="number"
            name="timeout"
            step={10}
            min={30}
            max={3600}
            value={extraSettings.timeout}
            oninput={(e) => {
              let val =
                e.currentTarget.value === ""
                  ? 300
                  : Number(e.currentTarget.value);
              // clamp value between 30 and 3600
              if (val < 30) val = 30;
              if (val > 3600) val = 3600;
              // ensure value is a multiple of 10
              if (val % 10 !== 0) val = Math.round(val / 10) * 10;
              dispatchChange("extraSettings.timeout", val);
            }}
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
          />
          <p class="mt-1 text-xs text-muted-foreground">
            If your {tabLabel} instance takes a long time to respond, you can increase
            this timeout value (in seconds). Default is 300 seconds.
          </p>
        </div>
      </div>
    {/if}

    <!-- request limit -->
    {#if "request_limit" in extraSettings}
      <div>
        <div>
          <label
            for="request_limit"
            class="block text-sm font-medium text-foreground mb-2"
            >Refresh request limit</label
          >
          <Input
            type="number"
            name="request_limit"
            step={10}
            min={100}
            max={999_999}
            value={extraSettings.request_limit}
            oninput={(e) => {
              let val =
                e.currentTarget.value === ""
                  ? Number(extraSettings.request_limit ?? 100)
                  : Number(e.currentTarget.value);
              // clamp value between 100 and 999_999
              if (val < 100) val = 100;
              if (val > 999_999) val = 999_999;
              dispatchChange("extraSettings.request_limit", Math.round(val));
            }}
            class="input-hover-el text-foreground placeholder:text-muted-foreground"
          />
          <p class="mt-1 text-xs text-muted-foreground">
            Maximum items this provider refreshes per task run. Lower this if
            your API quota is tight.
          </p>
        </div>
      </div>
    {/if}

    <!-- MDBList supporter mode -->
    {#if "supporter_mode" in extraSettings}
      <div>
        <label class="flex items-center gap-2 cursor-pointer">
          <Switch
            class="cursor-pointer"
            checked={Boolean(extraSettings.supporter_mode)}
            onCheckedChange={(checked) => {
              dispatchChange("extraSettings.supporter_mode", checked);
              dispatchChange(
                "extraSettings.request_delay_seconds",
                checked ? 0.2 : 1.0,
              );
            }}
          />
          <span class="text-sm font-medium text-foreground"
            >MDBList supporter mode</span
          >
        </label>
        <p class="mt-1 text-xs text-muted-foreground">
          Uses a faster default request delay of 0.2 seconds. Leave disabled for
          the standard 1 second delay.
        </p>
      </div>
    {/if}

    <!-- request delay -->
    {#if "request_delay_seconds" in extraSettings}
      <div>
        <label
          for="request_delay_seconds"
          class="block text-sm font-medium text-foreground mb-2"
          >Request delay</label
        >
        <Input
          type="number"
          name="request_delay_seconds"
          step={0.1}
          min={0}
          max={10}
          value={extraSettings.request_delay_seconds}
          oninput={(e) => {
            let val =
              e.currentTarget.value === ""
                ? Number(extraSettings.supporter_mode ? 0.2 : 1.0)
                : Number(e.currentTarget.value);
            if (val < 0) val = 0;
            if (val > 10) val = 10;
            dispatchChange(
              "extraSettings.request_delay_seconds",
              Math.round(val * 10) / 10,
            );
          }}
          class="input-hover-el text-foreground placeholder:text-muted-foreground"
        />
        <p class="mt-1 text-xs text-muted-foreground">
          Minimum seconds to wait between provider requests during a refresh.
        </p>
      </div>
    {/if}
  {/if}
</div>
