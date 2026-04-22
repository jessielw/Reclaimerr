<script lang="ts">
  import type { Component } from "svelte";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import { Input } from "$lib/components/ui/input/index.js";

  interface Props {
    tabLabel: string;
    tabIcon: Component | null;
    enabled: boolean;
    baseUrl: string;
    apiKey: string;
    apiKeyIsSet?: boolean;
    apiKeyLabel?: string;
    baseUrlPlaceholder?: string;
    disableToggle?: boolean;
    extraSettings?: Record<string, string>;
    onchange?: (event: CustomEvent) => void;
  }

  let {
    tabLabel,
    tabIcon,
    enabled,
    baseUrl,
    apiKey,
    apiKeyIsSet = false,
    apiKeyLabel = "API Key",
    baseUrlPlaceholder,
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
    <label for="baseUrl" class="block text-sm font-medium text-foreground mb-2"
      >Base URL</label
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

  <!-- extra settings (these can be customized per service but for now there is only timeout) -->
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
  {/if}
</div>
