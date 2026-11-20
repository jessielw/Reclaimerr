<script lang="ts">
  import { onMount } from "svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { Textarea } from "$lib/components/ui/textarea/index.js";
  import { Switch } from "$lib/components/ui/switch/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import TestButton from "$lib/components/test-button.svelte";
  import Save from "@lucide/svelte/icons/save";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
  import Shuffle from "@lucide/svelte/icons/shuffle";
  import FileCode from "@lucide/svelte/icons/file-code";
  import ChevronDown from "@lucide/svelte/icons/chevron-down";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import { delete_api, get_api, post_api, put_api } from "$lib/api";
  import { toast } from "svelte-sonner";

  interface TemplateEvent {
    event: string;
    label: string;
    custom: boolean;
    everyone: boolean;
  }
  interface TemplateList {
    events: TemplateEvent[];
    voices: string | null;
    example_voices: string;
  }
  interface TemplateDetail {
    event: string;
    custom: boolean;
    html: string;
    example_html: string;
  }
  interface Preview {
    subject: string;
    html: string;
  }

  let loading = $state(true);
  let events = $state<TemplateEvent[]>([]);
  let selected = $state("");
  let html = $state("");
  let savedHtml = $state("");
  let exampleHtml = $state("");
  let custom = $state(false);
  let voices = $state("");
  let savedVoices = $state<string | null>(null);
  let exampleVoices = $state("");
  let showVoices = $state(false);
  let saving = $state(false);
  let testing = $state(false);
  let testStatus = $state<"idle" | "success" | "error">("idle");
  let preview = $state<Preview | null>(null);
  let previewError = $state<string | null>(null);
  // which library movie the preview shows; changing it picks another one
  let seed = $state(Math.floor(Math.random() * 1_000_000));

  const dirty = $derived(html !== savedHtml);
  const everyone = $derived(
    events.find((e) => e.event === selected)?.everyone ?? false,
  );
  const voicesDirty = $derived(voices !== (savedVoices ?? exampleVoices));
  const message = (error: unknown) =>
    error instanceof Error ? error.message : String(error);

  // Until wording is saved or edited, emails use the built-in lines rather than
  // the example shown in the editor, so a preview must not send the example.
  const voicesForRequest = () =>
    voices.trim() && (savedVoices !== null || voices !== exampleVoices)
      ? voices
      : null;

  const base = "/api/settings/notifications/email-templates";

  async function loadList() {
    const list = await get_api<TemplateList>(base);
    events = list.events;
    exampleVoices = list.example_voices;
    savedVoices = list.voices;
    voices = list.voices ?? list.example_voices;
  }

  async function select(event: string) {
    selected = event;
    try {
      const detail = await get_api<TemplateDetail>(`${base}/${event}`);
      html = detail.html;
      savedHtml = detail.custom ? detail.html : "";
      exampleHtml = detail.example_html;
      custom = detail.custom;
    } catch (error) {
      toast.error(`Failed to load template: ${message(error)}`);
    }
    await refreshPreview();
  }

  // the preview grows to show the whole email instead of scrolling inside a box
  let previewHeight = $state(560);

  function fitPreview(event: Event) {
    const doc = (event.currentTarget as HTMLIFrameElement).contentDocument;
    const win = doc?.defaultView;
    if (!doc || !win) return;
    // no inner scrollbar: it would narrow the email and make it taller again
    doc.documentElement.style.overflow = "hidden";
    const measure = () => {
      previewHeight = Math.max(doc.documentElement.scrollHeight, 200);
    };
    measure();
    // images load after the document and change its height
    new win.ResizeObserver(measure).observe(doc.body);
  }

  let previewTimer: ReturnType<typeof setTimeout> | undefined;
  let previewSeq = 0;

  async function refreshPreview() {
    if (!selected) return;
    const seq = ++previewSeq;
    try {
      const result = await post_api<Preview>(`${base}/${selected}/preview`, {
        html,
        voices: voicesForRequest(),
        seed,
      });
      if (seq !== previewSeq) return;
      preview = result;
      previewError = null;
    } catch (error) {
      if (seq !== previewSeq) return;
      previewError = message(error);
    }
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(refreshPreview, 500);
  }

  async function save() {
    saving = true;
    try {
      await put_api(`${base}/${selected}`, { html });
      savedHtml = html;
      custom = true;
      events = events.map((e) =>
        e.event === selected ? { ...e, custom: true } : e,
      );
      toast.success("Template saved. New emails for this event will use it.");
    } catch (error) {
      toast.error(`Failed to save template: ${message(error)}`);
    } finally {
      saving = false;
    }
  }

  async function reset() {
    try {
      await delete_api(`${base}/${selected}`);
      html = exampleHtml;
      savedHtml = "";
      custom = false;
      events = events.map((e) =>
        e.event === selected ? { ...e, custom: false, everyone: false } : e,
      );
      toast.success("Template removed. This event sends the default email.");
      await refreshPreview();
    } catch (error) {
      toast.error(`Failed to remove template: ${message(error)}`);
    }
  }

  async function setEveryone(enabled: boolean) {
    try {
      await put_api(`${base}/${selected}/everyone`, { enabled });
      events = events.map((e) =>
        e.event === selected ? { ...e, everyone: enabled } : e,
      );
      toast.success(
        enabled
          ? "Every Email destination now gets this template for this event."
          : "Only destinations that choose Template get it.",
      );
    } catch (error) {
      toast.error(message(error));
    }
  }

  async function saveVoices() {
    try {
      await put_api("/api/settings/notifications/email-voices", {
        text: voices,
      });
      savedVoices = voices;
      toast.success("Wording saved");
    } catch (error) {
      toast.error(message(error));
    }
  }

  async function resetVoices() {
    try {
      await delete_api("/api/settings/notifications/email-voices");
      savedVoices = null;
      voices = exampleVoices;
      toast.success("Wording reset to the example");
      await refreshPreview();
    } catch (error) {
      toast.error(message(error));
    }
  }

  async function sendTest() {
    testing = true;
    testStatus = "idle";
    try {
      const result = await post_api<{ message: string }>(
        `${base}/${selected}/test`,
        { html, voices: voicesForRequest(), seed },
      );
      testStatus = "success";
      toast.success(result.message);
    } catch (error) {
      testStatus = "error";
      toast.error(message(error), { duration: 10000 });
    } finally {
      testing = false;
    }
  }

  async function init() {
    try {
      await loadList();
      if (events.length > 0) await select(events[0].event);
    } catch (error) {
      toast.error(`Failed to load email templates: ${message(error)}`);
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void init();
    return () => clearTimeout(previewTimer);
  });
</script>

<div class="bg-muted/50 border rounded-lg p-4 shadow-sm space-y-4 mb-4">
  <div>
    <h3 class="font-semibold text-foreground">Email templates</h3>
    <p class="text-sm text-muted-foreground">
      Replace the default text email for an event with your own HTML. Use
      placeholders like <code>{"{{media_title}}"}</code>,
      <code>{"{{poster_url}}"}</code> and <code>{"{{size}}"}</code>, and
      <code>{"{{#if genres}}...{{/if}}"}</code> for parts that may be empty. The
      <code>&lt;title&gt;</code> is the subject. After saving, choose
      <strong>Template</strong> for that event on an Email destination below. Destinations
      that keep Compact or Standard are unchanged.
    </p>
  </div>

  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner class="w-12 h-12 text-primary" />
    </div>
  {:else}
    <div class="flex flex-wrap gap-2">
      {#each events as item (item.event)}
        <Button
          variant={selected === item.event ? "default" : "outline"}
          size="sm"
          class="gap-2"
          onclick={() => select(item.event)}
        >
          {item.label}
          {#if item.custom}
            <Badge variant="secondary">Custom</Badge>
          {/if}
        </Button>
      {/each}
    </div>

    <div class="grid gap-4 lg:grid-cols-2">
      <div class="space-y-3">
        <Textarea
          bind:value={html}
          oninput={schedulePreview}
          spellcheck={false}
          rows={22}
          class="font-mono text-xs text-foreground"
          aria-label="Email template HTML"
        />
        <div class="flex flex-wrap gap-2">
          <Button onclick={save} disabled={saving || !dirty} class="gap-2">
            {#if saving}<Spinner class="w-4 h-4" />{:else}<Save />{/if}
            Save template
          </Button>
          <TestButton
            onclick={sendTest}
            loading={testing}
            status={testStatus}
            variant="secondary"
          >
            Send test email
          </TestButton>
          <Button
            variant="outline"
            class="gap-2"
            onclick={() => {
              html = exampleHtml;
              schedulePreview();
            }}
          >
            <FileCode /> Use example
          </Button>
          {#if custom}
            <Button variant="outline" class="gap-2" onclick={reset}>
              <RotateCcw /> Use default email
            </Button>
          {/if}
        </div>
        <label class="flex items-start gap-3 rounded-md border p-3">
          <Switch
            checked={everyone}
            disabled={!custom}
            onCheckedChange={(value) => setEveryone(value)}
          />
          <span>
            <span class="text-sm font-medium text-foreground">
              Use for everyone
            </span>
            <span class="block text-xs text-muted-foreground">
              Send this template to every Email destination for this event, no
              matter what each person's dropdown says. Without this, each person
              has to choose Template on their own destination.
              {#if !custom}Save a template first.{/if}
            </span>
          </span>
        </label>
        <p class="text-xs text-muted-foreground">
          {#if custom}
            This event sends your template.
          {:else}
            This event sends the default email until you save a template. The
            editor shows the example.
          {/if}
          The test email goes to your own account address and uses sample data.
        </p>
      </div>

      <div class="space-y-2">
        <div class="text-sm text-muted-foreground">
          Subject:
          <span class="text-foreground">{preview?.subject ?? ""}</span>
        </div>
        {#if previewError}
          <p class="text-sm text-destructive">{previewError}</p>
        {/if}
        <!-- allow-same-origin (without allow-scripts) lets us measure the
             email's height; the template still cannot run any script -->
        <iframe
          title="Email preview"
          sandbox="allow-same-origin"
          scrolling="no"
          srcdoc={preview?.html ?? ""}
          onload={fitPreview}
          style:height="{previewHeight}px"
          class="w-full rounded-md border bg-white"
        ></iframe>
        <div class="flex items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            class="gap-2"
            onclick={() => {
              seed += 1;
              refreshPreview();
            }}
          >
            <Shuffle /> Another title
          </Button>
          <p class="text-xs text-muted-foreground">
            Shows a movie from your library with made-up dates. Images load from
            the internet, as they do in a real email.
          </p>
        </div>
      </div>
    </div>

    <div class="border-t pt-3">
      <button
        type="button"
        class="flex items-center gap-1 text-sm font-medium text-foreground"
        onclick={() => (showVoices = !showVoices)}
      >
        {#if showVoices}<ChevronDown class="w-4 h-4" />{:else}<ChevronRight
            class="w-4 h-4"
          />{/if}
        Varied wording (optional)
      </button>
      {#if showVoices}
        <p class="text-sm text-muted-foreground mt-2">
          A JSON list of <code>headline</code>, <code>subline</code> and
          <code>subject</code> entries. One is picked per request and made
          available to templates as <code>{"{{headline}}"}</code>,
          <code>{"{{subline}}"}</code> and
          <code>{"{{voice_subject}}"}</code>. They can use the other
          placeholders. Templates do not need it, and it is shared by every
          template.
        </p>
        <Textarea
          bind:value={voices}
          oninput={schedulePreview}
          spellcheck={false}
          rows={10}
          class="font-mono text-xs text-foreground mt-2"
          aria-label="Email wording JSON"
        />
        <div class="flex gap-2 mt-2">
          <Button onclick={saveVoices} disabled={!voicesDirty} class="gap-2">
            <Save /> Save wording
          </Button>
          {#if savedVoices !== null}
            <Button variant="outline" class="gap-2" onclick={resetVoices}>
              <RotateCcw /> Use example wording
            </Button>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</div>
