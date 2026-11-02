<script lang="ts">
  import { untrack, tick } from "svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { fetchAPI } from "$lib/api";
  import { formatFileSize } from "$lib/utils/formatters";
  import DecisionEvidence from "./decision-evidence.svelte";
  import OperationEvidence from "./operation-evidence.svelte";
  import SupportCodeLookup from "./support-code-lookup.svelte";
  import type {
    BundlePreset,
    BundlePreview,
    BundleSection,
    CheckKind,
    DiagnosticCheckResult,
    DiagnosticsReport,
    DiagnosticsTarget,
    SupportMatch,
  } from "$lib/types/diagnostics";

  let {
    open,
    target,
    includePaths,
    services = [],
  }: {
    open: boolean;
    target?: DiagnosticsTarget;
    includePaths: boolean;
    services?: NonNullable<DiagnosticsReport["sections"]["services"]>;
  } = $props();
  const presets: Record<
    BundlePreset,
    { label: string; sections: BundleSection[] }
  > = {
    playback: {
      label: "Playback history",
      sections: ["services", "configuration", "playback", "timeline"],
    },
    candidate: {
      label: "Why this candidate?",
      sections: [
        "services",
        "configuration",
        "playback",
        "decisions",
        "timeline",
      ],
    },
    deletion: {
      label: "Deletion or move failure",
      sections: [
        "services",
        "configuration",
        "storage",
        "decisions",
        "tasks",
        "timeline",
      ],
    },
    paths: {
      label: "Paths and mounts",
      sections: ["services", "configuration", "storage"],
    },
    system: {
      label: "System and tasks",
      sections: ["services", "configuration", "tasks", "timeline"],
    },
  };
  const labels: Record<BundleSection, string> = {
    services: "Services and libraries",
    configuration: "Relevant settings",
    storage: "Storage and children",
    playback: "Playback evidence",
    decisions: "Candidate decisions and traces",
    tasks: "Schedules and tasks",
    timeline: "Failure timeline",
  };
  let preset = $state<BundlePreset>(
    untrack(() => (target ? "candidate" : "system")),
  );
  let sections = $state<BundleSection[]>(
    untrack(() => [...presets[preset].sections]),
  );
  let days = $state(7);
  let eventLimit = $state(50);
  let includeSqlite = $state(false);
  let includeLogs = $state(false);
  let preview = $state<BundlePreview | null>(null);
  let preparedKey = $state("");
  let preparing = $state(false);
  let downloading = $state(false);
  let bundleError = $state("");
  let checkKind = $state<CheckKind>(
    untrack(() => (target ? "relationships" : "provider")),
  );
  let serviceId = $state("");
  let checking = $state(false);
  let checkError = $state("");
  let checkResult = $state<DiagnosticCheckResult | null>(null);
  let previewController: AbortController | undefined;
  let checkController: AbortController | undefined;
  let downloadController: AbortController | undefined;
  let bundleTarget = $state<DiagnosticsTarget | undefined>(
    untrack(() => target),
  );
  let supportCode = $state<string | undefined>();
  let bundleOpen = $state(false);
  let bundlePanel: HTMLDetailsElement | undefined;
  let lookupRequest = $state<{ code: string; revision: number }>();
  const optionsKey = $derived(
    JSON.stringify({
      target: bundleTarget,
      support_code: supportCode,
      preset,
      sections,
      history_days: days,
      event_limit: eventLimit,
      include_file_paths: includePaths,
      include_sqlite: includeSqlite,
      include_logs: includeLogs,
    }),
  );
  const ready = $derived(
    !!preview && preparedKey === optionsKey && !preparing && !bundleError,
  );
  const previewJson = $derived(
    preview
      ? JSON.stringify(
          {
            manifest: preview.manifest,
            report: preview.report,
            traces: preview.traces,
            timeline: preview.timeline,
            logs: preview.logs,
          },
          null,
          2,
        )
      : "",
  );

  $effect(() => {
    void includePaths;
    untrack(() => {
      checkController?.abort();
      checking = false;
      checkResult = null;
      checkError = "";
    });
  });

  $effect(() => {
    void open;
    void target?.scope;
    void target?.id;
    untrack(() => {
      preview = null;
      preparedKey = "";
      checkResult = null;
      bundleError = "";
      checkError = "";
      preparing = checking = downloading = false;
      bundleTarget = target;
      supportCode = undefined;
      lookupRequest = undefined;
      if (!target) {
        preset = "system";
        sections = [...presets.system.sections];
        checkKind = "provider";
      }
    });
    return () => {
      previewController?.abort();
      checkController?.abort();
      downloadController?.abort();
    };
  });

  function choosePreset(value: BundlePreset) {
    preset = value;
    sections = [...presets[value].sections];
  }
  function lookup(code: string) {
    lookupRequest = { code, revision: (lookupRequest?.revision ?? 0) + 1 };
  }
  function prepareMatch(
    match: SupportMatch,
    chosenTarget: DiagnosticsTarget | null,
  ) {
    bundleTarget = chosenTarget ?? undefined;
    supportCode = match.code;
    preset = match.suggested.preset;
    sections = [...match.suggested.sections];
    days = match.suggested.history_days;
    bundleOpen = true;
    void prepare();
    void tick().then(() => bundlePanel?.scrollIntoView({ block: "nearest" }));
  }
  export async function showCandidateEvidence() {
    if (!target || preparing || downloading) return;
    supportCode = undefined;
    bundleTarget = target;
    choosePreset("candidate");
    bundleOpen = true;
    await tick();
    bundlePanel?.scrollIntoView({ block: "nearest" });
    await prepare();
  }
  function failure(status: number) {
    return status === 429
      ? "Diagnostics are busy. Try again shortly."
      : status === 403
        ? "Administrator access is required."
        : status === 404
          ? "This preview expired or is unavailable. Prepare a new preview."
          : "Could not complete the request. Try again when the app is available.";
  }
  async function prepare() {
    previewController?.abort();
    const controller = new AbortController();
    previewController = controller;
    const key = optionsKey;
    preparing = true;
    bundleError = "";
    const timeout = setTimeout(() => {
      controller.abort();
      preparing = false;
      bundleError = "Preparation timed out. Try again with fewer sections.";
    }, 20000);
    try {
      const response = await fetchAPI("/api/diagnostics/bundles/preview", {
        method: "POST",
        body: key,
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(failure(response.status));
      const result: BundlePreview = await response.json();
      if (controller.signal.aborted) return;
      preview = result;
      preparedKey = key;
    } catch (error) {
      if (!controller.signal.aborted)
        bundleError =
          error instanceof Error
            ? error.message
            : "Could not prepare the bundle.";
    } finally {
      clearTimeout(timeout);
      if (!controller.signal.aborted) preparing = false;
    }
  }
  function save(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function download() {
    if (!preview || !ready) return;
    const snapshot = preview;
    const controller = new AbortController();
    downloadController = controller;
    downloading = true;
    const timeout = setTimeout(() => {
      controller.abort();
      downloading = false;
      bundleError = "Download timed out. Prepare a new preview and try again.";
    }, 20000);
    try {
      const response = await fetchAPI(
        `/api/diagnostics/bundles/${encodeURIComponent(snapshot.download_token)}`,
        { signal: controller.signal },
      );
      if (!response.ok) throw new Error(failure(response.status));
      const blob = await response.blob();
      if (!controller.signal.aborted)
        save(blob, `reclaimerr-support-${snapshot.manifest.report_id}.zip`);
    } catch (error) {
      if (!controller.signal.aborted)
        bundleError =
          error instanceof Error
            ? error.message
            : "Could not download the bundle.";
    } finally {
      clearTimeout(timeout);
      if (!controller.signal.aborted) downloading = false;
    }
  }
  async function runCheck() {
    const controller = new AbortController();
    checkController = controller;
    checking = true;
    checkError = "";
    checkResult = null;
    const timeout = setTimeout(() => {
      controller.abort();
      checking = false;
      checkError = "The check timed out. Its result is unknown.";
    }, 15000);
    try {
      const response = await fetchAPI("/api/diagnostics/checks", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          kind: checkKind,
          target,
          include_file_paths: includePaths,
          ...(checkKind === "provider"
            ? { service_config_id: Number(serviceId) }
            : {}),
        }),
      });
      if (!response.ok) throw new Error(failure(response.status));
      const result: DiagnosticCheckResult = await response.json();
      if (!controller.signal.aborted) checkResult = result;
    } catch (error) {
      if (!controller.signal.aborted)
        checkError =
          error instanceof Error ? error.message : "Could not run the check.";
    } finally {
      clearTimeout(timeout);
      if (!controller.signal.aborted) checking = false;
    }
  }
</script>

<SupportCodeLookup
  {open}
  request={lookupRequest}
  busy={preparing || downloading}
  onprepare={prepareMatch}
/>

<details
  bind:this={bundlePanel}
  bind:open={bundleOpen}
  class="rounded-md border p-3 space-y-3"
>
  <summary class="cursor-pointer text-sm font-medium"
    >Support bundle - choose what to share</summary
  >
  <p class="text-sm text-muted-foreground">
    Choose evidence for this {bundleTarget
      ? `${bundleTarget.scope.replaceAll("_", " ")} #${bundleTarget.id}`
      : "system"}. App information and selected media identity are always
    included. Nothing is uploaded.
  </p>
  {#if supportCode}<div class="rounded border p-2 space-y-2 text-sm">
      <p class="break-all">
        History filtered to <span class="font-mono">{supportCode}</span>.
        Current settings and media context are collected separately.
      </p>
      <Button
        variant="outline"
        size="sm"
        disabled={preparing || downloading}
        onclick={() => {
          supportCode = undefined;
          bundleTarget = target;
          choosePreset(target ? "candidate" : "system");
        }}>Return to {target ? "this item" : "system"}</Button
      >
    </div>{/if}
  <fieldset disabled={preparing || downloading} class="space-y-3">
    <div class="grid gap-3 sm:grid-cols-3">
      <label class="text-sm"
        >Problem
        <select
          class="mt-1 block w-full rounded-md border bg-background p-2"
          value={preset}
          onchange={(e) => choosePreset(e.currentTarget.value as BundlePreset)}
        >
          {#each Object.entries(presets) as [key, value]}
            {#if bundleTarget || supportCode || key === "system"}<option
                value={key}>{value.label}</option
              >{/if}
          {/each}
        </select>
      </label>
      <label class="text-sm"
        >History window
        <select
          class="mt-1 block w-full rounded-md border bg-background p-2"
          bind:value={days}
        >
          <option value={1}>Last 24 hours</option><option value={7}
            >Last 7 days</option
          ><option value={14}>Last 14 days</option><option value={30}
            >Last 30 days</option
          >
        </select>
      </label>
      <label class="text-sm"
        >Playback sample limit
        <select
          class="mt-1 block w-full rounded-md border bg-background p-2"
          bind:value={eventLimit}
        >
          <option value={10}>10 events</option><option value={50}
            >50 events</option
          ><option value={100}>100 events</option>
        </select>
      </label>
    </div>
    <div class="grid gap-2 sm:grid-cols-2">
      {#each Object.entries(labels) as [key, label]}
        {#if bundleTarget || supportCode || !["storage", "decisions"].includes(key)}
          <label class="flex gap-2 text-sm"
            ><input
              type="checkbox"
              value={key}
              bind:group={sections}
            />{label}</label
          >
        {/if}
      {/each}
    </div>
    <label class="flex items-start gap-2 text-sm"
      ><input type="checkbox" class="mt-1" bind:checked={includeSqlite} /><span
        >Include sanitized SQLite extract<span
          class="block text-muted-foreground"
          >Contains only the preview's records in support tables. Cannot restore
          Reclaimerr.</span
        ></span
      ></label
    >
    <label class="flex items-start gap-2 text-sm"
      ><input type="checkbox" class="mt-1" bind:checked={includeLogs} /><span
        >Include sanitized log summaries<span
          class="block text-muted-foreground"
          >Bounded recent log tails with times, levels, failure categories and
          correlated IDs. Original message text is excluded.</span
        ></span
      ></label
    >
    <p class="text-xs text-muted-foreground">
      Paths are {includePaths
        ? "included with account and host masking"
        : "masked"}, using the option above. The history window applies to
      events; current state and cumulative playback totals can be older.
      Decision conditions can also contain playback evidence. Schedules and
      recent task runs describe app-wide activity.
    </p>
  </fieldset>
  <div class="flex flex-wrap gap-2">
    <Button
      variant="outline"
      disabled={preparing || downloading}
      onclick={prepare}
      >{preparing
        ? "Preparing preview…"
        : preview
          ? "Refresh bundle preview"
          : "Prepare bundle preview"}</Button
    >
    <Button disabled={!ready || downloading} onclick={download}
      >{downloading ? "Downloading…" : "Download previewed ZIP"}</Button
    >
  </div>
  <div aria-live="polite">
    {#if bundleError}<p class="text-sm text-destructive" role="alert">
        {bundleError}
      </p>{/if}
    {#if preview}
      {#if preparedKey !== optionsKey}<p
          class="text-sm text-amber-600 dark:text-amber-400"
        >
          Options changed. Refresh the preview before downloading.
        </p>{/if}
      <div class="mt-3 space-y-3 text-sm">
        <p>
          {formatFileSize(preview.archive_bytes)} · Download available until {new Date(
            preview.expires_at,
          ).toLocaleTimeString()}. This download uses the exact prepared
          snapshot.
        </p>
        <p>Files: {preview.manifest.files.join(", ")}</p>
        <details>
          <summary class="cursor-pointer font-medium"
            >Sharing inventory and limits</summary
          >
          <table class="mt-2 w-full text-left">
            <thead><tr><th class="py-1">Section</th><th>Records</th></tr></thead
            ><tbody>
              {#each preview.manifest.inventory as item}<tr
                  ><td class="py-1">{item.section.replaceAll("_", " ")}</td><td
                    >{item.records}</td
                  ></tr
                >{/each}
            </tbody>
          </table>
          <p class="mt-2">
            Excluded: {preview.manifest.excluded.join(", ") ||
              "No optional sections excluded"}.
          </p>
          <ul class="mt-2 list-disc pl-5">
            {#each preview.manifest.limitations as limit}<li>{limit}</li>{/each}
          </ul>
          <p class="mt-2 break-all font-mono text-xs">
            SHA-256: {preview.sha256}
          </p>
        </details>
        {#if preview.report.sections.decisions || preview.traces.length}<DecisionEvidence
            decisions={preview.report.sections.decisions}
            traces={preview.traces}
            onlookup={lookup}
          />{/if}
        <OperationEvidence entries={preview.timeline} onlookup={lookup} />
        {#if sections.includes("decisions") && !preview.traces.length}<p
            class="text-muted-foreground"
          >
            No recorded decision traces in this snapshot. Older scans and
            targets skipped before evaluation have no trace; missing evidence is
            not a failed rule.
          </p>{/if}
        {#if preview.timeline.length}<details>
            <summary class="cursor-pointer font-medium"
              >Timeline ({preview.timeline.length} events)</summary
            >
            <ol class="mt-2 space-y-2">
              {#each preview.timeline as event}<li>
                  <span class="text-muted-foreground"
                    >{new Date(event.occurred_at).toLocaleString()}</span
                  >
                  - {event.event.replaceAll("_", " ")}{event.error_category
                    ? ` · ${event.error_category.replaceAll("_", " ")}`
                    : ""}<span class="block break-all font-mono text-xs"
                    >{event.correlation_id}</span
                  >
                  <Button
                    variant="outline"
                    size="sm"
                    onclick={() =>
                      lookup(
                        event.job_id && event.correlation_id.startsWith("job-")
                          ? `job-${event.job_id}`
                          : event.event.startsWith("rule_")
                            ? `scan-${event.correlation_id}`
                            : `operation-${event.correlation_id}`,
                      )}
                    disabled={!/^[0-9a-f-]{36}$/.test(event.correlation_id) &&
                      !event.correlation_id.startsWith("job-")}
                    >Look up code</Button
                  >
                </li>{/each}
            </ol>
          </details>{/if}
        <details>
          <summary class="cursor-pointer font-medium"
            >Full bundle data preview</summary
          ><textarea
            aria-label="Support bundle JSON preview"
            readonly
            value={previewJson}
            class="mt-2 h-64 w-full rounded-md border bg-muted p-3 font-mono text-xs"
            spellcheck="false"></textarea>
        </details>
      </div>
    {/if}
  </div>
</details>

<details class="rounded-md border p-3 space-y-3">
  <summary class="cursor-pointer text-sm font-medium"
    >Run a diagnostic check</summary
  >
  <p class="text-sm text-muted-foreground">
    Checks run only when requested. Path checks read filesystem metadata;
    provider checks contact the saved server. Each phase has a five-second
    budget. No repairs, syncs or cleanup run.
  </p>
  <fieldset disabled={checking} class="grid gap-3 sm:grid-cols-2">
    <label class="text-sm"
      >Check
      <select
        class="mt-1 block w-full rounded-md border bg-background p-2"
        bind:value={checkKind}
        onchange={() => {
          checkResult = null;
        }}
      >
        {#if target}<option value="relationships">Stored relationships</option
          ><option value="path">Stored paths and mappings</option>{/if}
        <option value="provider">Provider connection</option>
      </select>
    </label>
    {#if checkKind === "provider"}<label class="text-sm"
        >Configured service<select
          class="mt-1 block w-full rounded-md border bg-background p-2"
          bind:value={serviceId}
          onchange={() => {
            checkResult = null;
          }}
          ><option value="">Choose a service</option
          >{#each services as service}<option value={String(service.id)}
              >{service.alias} · {service.service}{service.enabled
                ? ""
                : " (disabled)"}</option
            >{/each}</select
        ></label
      >{/if}
  </fieldset>
  <Button
    variant="outline"
    disabled={checking || (checkKind === "provider" && !serviceId)}
    onclick={runCheck}>{checking ? "Checking…" : "Run selected check"}</Button
  >
  <div aria-live="polite">
    {#if checkError}<p class="text-sm text-destructive" role="alert">
        {checkError}
      </p>{/if}
    {#if checkResult}
      <p class="mt-2 text-xs text-muted-foreground">
        {checkResult.kind} check · {new Date(
          checkResult.checked_at,
        ).toLocaleString()} · Separate from the bundle snapshot.
      </p>
      <ul class="mt-2 space-y-2 text-sm">
        {#each checkResult.observations as observation}<li>
            <strong
              >{observation.status.toUpperCase()} · {observation.subject}</strong
            >
            <p class="text-muted-foreground">{observation.detail}</p>
          </li>{/each}
      </ul>
      <Button
        class="mt-3"
        variant="outline"
        onclick={() => {
          if (checkResult)
            save(
              new Blob([JSON.stringify(checkResult, null, 2)], {
                type: "application/json",
              }),
              `reclaimerr-check-${checkResult.check_id}.json`,
            );
        }}>Download check result</Button
      >
    {/if}
  </div>
</details>
