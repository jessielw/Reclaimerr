<script lang="ts">
  import { onMount } from "svelte";
  import type { Component } from "svelte";
  import Markdown from "svelte-exmarkdown";
  import { gfmPlugin } from "svelte-exmarkdown/gfm";

  import { get_api } from "$lib/api";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import * as Select from "$lib/components/ui/select/index.js";

  interface Release {
    version: string;
    date: string | null;
    body: string;
  }

  interface Props {
    svgIcon: Component | null;
  }
  let { svgIcon }: Props = $props();

  const mdPlugins = [gfmPlugin()];

  let loading = $state(true);
  let error = $state<string | null>(null);
  let releases = $state<Release[]>([]);
  let selectedIndex = $state(0);
  let currentVersion = $state<string | null>(null);

  const selected = $derived(releases[selectedIndex] ?? null);

  onMount(async () => {
    try {
      const [rel, ver] = await Promise.all([
        get_api<Release[]>("/api/info/changelog"),
        get_api<{ version: string }>("/api/info/version"),
      ]);
      releases = rel;
      currentVersion = ver.version;
      // default to the entry matching the running version, else first
      const match = releases.findIndex((r) => r.version === currentVersion);
      selectedIndex = match >= 0 ? match : 0;
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to load changelog.";
    } finally {
      loading = false;
    }
  });
</script>

<div class="space-y-6">
  <div>
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {#if svgIcon}
        {@const Icon = svgIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      <span class="align-middle">About</span>
    </h2>
  </div>

  {#if loading}
    <div class="flex items-center justify-center py-8">
      <Spinner class="text-primary" />
    </div>
  {:else if error}
    <div class="rounded-md border border-destructive/40 bg-destructive/10 p-3">
      <p class="text-sm text-destructive">{error}</p>
    </div>
  {:else}
    <!-- release notes section -->
    <section class="flex flex-col gap-3">
      <h3 class="text-foreground">Release Notes</h3>
      <p class="text-sm text-muted-foreground">
        Select below to view the release notes for each version
      </p>
      <Select.Root
        type="single"
        value={String(selectedIndex)}
        onValueChange={(v) => (selectedIndex = Number(v))}
      >
        <Select.Trigger class="w-1/3 cursor-pointer text-foreground">
          {#if selected}
            <span class="flex items-center gap-2">
              {selected.version}
              {#if selected.version === currentVersion}
                <Badge class="h-4 px-1 text-[10px] leading-none">Current</Badge>
              {/if}
              {#if selected.date}
                <span class="text-xs text-muted-foreground"
                  >{selected.date}</span
                >
              {/if}
            </span>
          {/if}
        </Select.Trigger>
        <Select.Content>
          {#each releases as release, i}
            <Select.Item value={String(i)} class="cursor-pointer">
              <span class="flex items-center gap-2">
                {release.version}
                {#if release.version === currentVersion}
                  <Badge class="h-4 px-1 text-[10px] leading-none"
                    >Current</Badge
                  >
                {/if}
                {#if release.date}
                  <span class="text-xs text-muted-foreground"
                    >{release.date}</span
                  >
                {/if}
              </span>
            </Select.Item>
          {/each}
        </Select.Content>
      </Select.Root>

      {#if selected}
        <div class="bg-muted/50 border rounded-lg p-4 shadow-sm">
          <article class="prose prose-sm dark:prose-invert max-w-none">
            <Markdown md={selected.body} plugins={mdPlugins} />
          </article>
        </div>
      {/if}
    </section>
  {/if}
</div>
