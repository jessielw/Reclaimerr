<script lang="ts">
  import { untrack } from "svelte";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { fetchAPI } from "$lib/api";
  import { formatFileSize } from "$lib/utils/formatters";
  import SupportTools from "./support-tools.svelte";
  import DecisionEvidence from "./decision-evidence.svelte";
  import type {
    DiagnosticsReport,
    DiagnosticsTarget,
  } from "$lib/types/diagnostics";

  let {
    open = $bindable(),
    target,
  }: { open: boolean; target?: DiagnosticsTarget } = $props();
  let selected = $state<DiagnosticsTarget | undefined>(untrack(() => target));
  let parents = $state<DiagnosticsTarget[]>([]);
  let includePaths = $state(false);
  let report = $state<DiagnosticsReport | null>(null);
  let snapshot = $state("");
  let loading = $state(false);
  let error = $state("");
  let copyStatus = $state("");
  let previewOpen = $state(false);
  let revision = $state(0);
  let childrenOffset = $state(0);
  let supportTools = $state<{ showCandidateEvidence: () => Promise<void> }>();
  const exportReady = $derived(!loading && !error && !!snapshot);

  $effect(() => {
    const options = {
      include_file_paths: includePaths,
      ...(selected
        ? { target: selected, children_offset: childrenOffset }
        : {}),
    };
    const kind = selected ? "media" : "system";
    void revision;
    if (!open) return;
    const controller = new AbortController();
    const timeout = setTimeout(() => {
      controller.abort();
      error = "The request timed out. Try again when the app is available.";
      loading = false;
    }, 15000);
    loading = true;
    error = "";
    copyStatus = "";
    // Keep the displayed snapshot mounted while its replacement is collected.
    // Export actions stay disabled until the requested options succeed.
    void (async () => {
      try {
        const response = await fetchAPI(`/api/diagnostics/${kind}`, {
          method: "POST",
          body: JSON.stringify(options),
          signal: controller.signal,
        });
        if (!response.ok)
          throw new Error(
            response.status === 429
              ? "Diagnostics are busy. Wait a few seconds and try again."
              : response.status === 403
                ? "Administrator access is required."
                : "Could not collect diagnostics. Try again when the app is available.",
          );
        const result: DiagnosticsReport = await response.json();
        if (controller.signal.aborted) return;
        report = result;
        snapshot = JSON.stringify(result, null, 2);
      } catch (e) {
        if (!controller.signal.aborted)
          error =
            e instanceof Error ? e.message : "Could not collect diagnostics.";
      } finally {
        clearTimeout(timeout);
        if (!controller.signal.aborted) loading = false;
      }
    })();
    return () => {
      clearTimeout(timeout);
      controller.abort();
    };
  });

  async function copy() {
    if (!exportReady) return;
    try {
      await navigator.clipboard.writeText(snapshot);
      copyStatus = "Debug info copied.";
    } catch {
      previewOpen = true;
      copyStatus =
        "Clipboard access is unavailable. Select the JSON below to copy it, or download the file.";
    }
  }

  function download() {
    if (!report || !exportReady) return;
    const url = URL.createObjectURL(
      new Blob([snapshot], { type: "application/json;charset=utf-8" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `reclaimerr-${report.report_kind}-${report.report_id}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function focusTarget(child: DiagnosticsTarget) {
    if (selected) parents = [...parents, selected];
    selected = { scope: child.scope, id: child.id };
    childrenOffset = 0;
  }
</script>

<Dialog.Root bind:open>
  <Dialog.Content class="sm:max-w-3xl max-h-[90vh] overflow-y-auto">
    <Dialog.Header>
      <Dialog.Title
        >{selected ? "Media debug info" : "Support diagnostics"}</Dialog.Title
      >
      <Dialog.Description>
        Includes media titles, IDs and playback information. Accounts, server
        names and addresses are masked. Review the preview before sharing.
        Nothing is uploaded automatically.
      </Dialog.Description>
    </Dialog.Header>

    <label class="flex items-start gap-2 text-sm">
      <input
        type="checkbox"
        bind:checked={includePaths}
        disabled={loading}
        class="mt-1"
      />
      <span
        >Include file paths <span class="block text-muted-foreground"
          >For mount and path-mapping problems. Paths may reveal folder names;
          credentials remain excluded.</span
        ></span
      >
    </label>
    {#if parents.length}
      <Button
        variant="outline"
        disabled={loading}
        onclick={() => {
          selected = parents[parents.length - 1];
          parents = parents.slice(0, -1);
          childrenOffset = 0;
        }}
        >Back to {parents[parents.length - 1].scope.replaceAll(
          "_",
          " ",
        )}</Button
      >
    {/if}

    <div aria-live="polite" aria-busy={loading} class="space-y-3">
      <p class="min-h-5 text-sm text-muted-foreground" role="status">
        {#if loading}
          {report
            ? "Updating report… Previous preview shown."
            : "Collecting stored diagnostics…"}
        {/if}
      </p>
      {#if error}
        <p role="alert" class="text-sm text-destructive">{error}</p>
        {#if report}
          <p class="text-sm text-muted-foreground">
            The previous preview is still shown. Retry to apply your current
            options before exporting.
          </p>
        {/if}
        <Button variant="outline" onclick={() => revision++}>Try again</Button>
      {/if}
      {#if report}
        <div class="rounded-md border p-3 space-y-2 text-sm">
          {#each report.summary as line}<p>{line}</p>{/each}
          <p class="text-muted-foreground">
            Collected {new Date(report.generated_at).toLocaleString()}
          </p>
        </div>
        {#if report.collection.partial}
          <details class="rounded-md border border-amber-500/50 p-3 text-sm">
            <summary class="cursor-pointer font-medium"
              >Partial report - view unavailable or limited sections</summary
            >
            <ul class="mt-2 list-disc pl-5">
              {#each report.collection.observations.filter((o) => o.state !== "available") as observation}
                <li>
                  {observation.section}: {observation.reason?.replaceAll(
                    "_",
                    " ",
                  )}
                </li>
              {/each}
              {#each report.collection.truncated as limit}<li>
                  {limit.section}: {limit.returned} of {limit.total ?? "more"} records
                  included
                </li>{/each}
              {#each report.collection.omitted as omitted}<li>
                  {omitted}
                </li>{/each}
            </ul>
          </details>
        {/if}
        {#if report.sections.children?.length}
          <label class="text-sm space-y-1">
            <span
              >Focus on an affected {report.target?.scope === "series"
                ? "season"
                : report.target?.scope === "season"
                  ? "episode"
                  : "version"}</span
            >
            <select
              class="block w-full rounded-md border bg-background p-2"
              value=""
              disabled={loading || !!error}
              onchange={(e) => {
                const child = report?.sections.children?.find(
                  (c) => String(c.id) === e.currentTarget.value,
                );
                if (child) focusTarget(child);
              }}
            >
              <option value="">Keep this report’s scope</option>
              {#each report.sections.children as child, index}
                <option value={child.id}
                  >{child.scope === "season"
                    ? `Season ${child.season_number}`
                    : child.scope === "episode"
                      ? `Episode ${child.episode_number}: ${child.title ?? ""}`
                      : `Version ${report.collection.children_offset + index + 1} · ${child.video_width && child.video_height ? `${child.video_width}×${child.video_height}` : "Unknown resolution"} · ${formatFileSize(child.size)}`}</option
                >
              {/each}
            </select>
          </label>
        {/if}
        {#if (report.collection.children_total ?? 0) > 100}
          <div class="flex items-center gap-2 text-sm">
            <Button
              variant="outline"
              disabled={loading || !!error || childrenOffset === 0}
              onclick={() =>
                (childrenOffset = Math.max(0, childrenOffset - 100))}
              >Previous 100</Button
            >
            <span
              >{report.collection.children_offset + 1}–{Math.min(
                report.collection.children_offset + 100,
                report.collection.children_total ?? 0,
              )} of {report.collection.children_total}</span
            >
            <Button
              variant="outline"
              disabled={loading ||
                !!error ||
                childrenOffset + 100 >= (report.collection.children_total ?? 0)}
              onclick={() => (childrenOffset += 100)}>Next 100</Button
            >
          </div>
        {/if}
        {#if report.sections.decisions}<DecisionEvidence
            decisions={report.sections.decisions}
            loading={!exportReady}
            onload={() => {
              void supportTools?.showCandidateEvidence();
            }}
          />{/if}
        <details bind:open={previewOpen}>
          <summary class="cursor-pointer text-sm font-medium"
            >JSON preview</summary
          >
          <textarea
            aria-label="Diagnostics JSON"
            readonly
            disabled={loading || !!error}
            value={snapshot}
            class="mt-2 h-64 w-full rounded-md border bg-muted p-3 font-mono text-xs"
            spellcheck="false"></textarea>
        </details>
      {/if}
      {#if copyStatus}<p class="text-sm" role="status">{copyStatus}</p>{/if}
    </div>
    <SupportTools
      bind:this={supportTools}
      {open}
      target={selected}
      {includePaths}
      services={report?.sections.services ?? []}
    />
    <Dialog.Footer class="flex-wrap gap-2">
      <Button variant="outline" onclick={() => (open = false)}>Close</Button>
      <Button
        variant="outline"
        disabled={loading || !snapshot}
        onclick={() => revision++}>Refresh report</Button
      >
      <Button variant="outline" disabled={!exportReady} onclick={download}
        >Download JSON</Button
      >
      <Button disabled={!exportReady} onclick={copy}>Copy debug info</Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
