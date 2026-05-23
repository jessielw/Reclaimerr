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
      <span class="align-middle">About Reclaimerr</span>
    </h2>
  </div>

  <hr />

  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner class="w-12 h-12 text-primary" />
    </div>
  {:else if error}
    <div class="rounded-md border border-destructive/40 bg-destructive/10 p-3">
      <p class="text-sm text-destructive">{error}</p>
    </div>
  {:else}
    <!-- support section -->
    <section class="flex flex-col gap-3">
      <h3 class="text-foreground">Getting Support</h3>
      <!-- docs -->
      <article>
        <h2>Documentation</h2>
        <p class="text-sm">Coming soon™</p>
      </article>

      <!-- github discussions -->
      <article>
        <h2>GitHub Discussions</h2>
        <p class="text-sm">
          For questions, troubleshooting, or just to chat with the community,
          check out our <a
            href="https://github.com/jessielw/Reclaimerr/discussions"
            target="_blank"
            rel="noopener noreferrer"
            class="text-primary underline"
          >
            GitHub Discussions
          </a>
        </p>
      </article>

      <!-- matrix -->
      <article>
        <h2>Matrix Chat</h2>
        <p class="text-sm">
          Join our Matrix room for real time support and discussion:
          <a
            href="https://matrix.to/#/#reclaimerr:matrix.org"
            target="_blank"
            rel="noopener noreferrer"
            class="text-primary underline"
          >
            #reclaimerr:matrix.org
          </a>
        </p>
      </article>

      <!-- fluxer -->
      <article>
        <h2>Fluxer</h2>
        <p class="text-sm">
          Once Fluxer V2 is released, we'll have a Reclaimerr channel there as
          well for real time chat. Stay tuned!
        </p>
      </article>
    </section>
    <hr />

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
        <Select.Trigger class="cursor-pointer text-foreground">
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
