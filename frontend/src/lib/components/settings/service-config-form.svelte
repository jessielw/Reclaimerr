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
</div>
