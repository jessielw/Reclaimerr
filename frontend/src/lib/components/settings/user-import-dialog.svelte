<script lang="ts">
  import { fetchAPI, post_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import ErrorBox from "$lib/components/error-box.svelte";

  let {
    open = $bindable(false),
    onImported,
  }: {
    open: boolean;
    onImported: () => Promise<void>;
  } = $props();
  interface ImportResult {
    rows: { row: number; username: string; status: string; message: string }[];
    ready: number;
    skipped: number;
    errors: number;
    created: number;
  }
  let file: File | null = $state(null);
  let result: ImportResult | null = $state(null);
  let busy = $state(false);
  let error = $state("");
  let completed = $state(false);

  async function downloadTemplate() {
    try {
      error = "";
      const response = await fetchAPI("/api/account/users/import/template");
      if (!response.ok) throw new Error("Could not download template");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "reclaimerr-users-template.csv";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      error =
        err instanceof Error ? err.message : "Could not download template";
    }
  }

  async function submit(preview: boolean) {
    if (!file || busy) return;
    busy = true;
    error = "";
    try {
      const data = new FormData();
      data.append("file", file);
      result = await post_api<ImportResult>(
        "/api/account/users/import?preview=" + preview,
        data,
      );
      completed = !preview && result.errors === 0 && result.ready === 0;
      if (result.created > 0) await onImported();
    } catch (err) {
      error = err instanceof Error ? err.message : "Import failed";
      result = null;
    } finally {
      busy = false;
    }
  }
</script>

<Dialog.Root bind:open>
  <Dialog.Content
    class="max-w-3xl max-h-[90vh] overflow-y-auto bg-card text-foreground"
  >
    <Dialog.Header>
      <Dialog.Title>Import Users from CSV</Dialog.Title>
      <Dialog.Description>
        Download the template, fill in your users, and preview before importing.
      </Dialog.Description>
    </Dialog.Header>
    <div class="space-y-4">
      <Button variant="secondary" onclick={downloadTemplate} disabled={busy}>
        Download CSV Template
      </Button>
      <div class="rounded-md border border-border p-3 text-sm space-y-2">
        <p>
          Keep the header: <code>username,email,display_name,password</code>.
        </p>
        <p>
          Username: 5–32 letters, numbers, underscores, or hyphens. Optional
          display name: 3–32 characters.
        </p>
        <p>
          Password: 3–64 characters, or leave blank for media sign-in. Email is
          required when password is blank. Media sign-in uses existing account
          matching; importing does not verify or link a media identity.
        </p>
        <p>
          Supplied passwords must be changed at first local login. New accounts
          receive the Request permission and configured default page access.
        </p>
        <p>
          Existing accounts are skipped. Conflicts and invalid rows must be
          corrected before any users are imported. UTF-8 CSV only; maximum 1 MB
          and 1,000 users.
        </p>
      </div>
      <div class="space-y-2">
        <Label for="user-import-file">CSV file</Label>
        <Input
          id="user-import-file"
          type="file"
          accept=".csv,text/csv"
          disabled={busy}
          onchange={(event) => {
            file = event.currentTarget.files?.[0] ?? null;
            result = null;
            error = "";
            completed = false;
          }}
        />
      </div>
      <ErrorBox {error} />
      {#if result}
        <p class="text-sm" role="status">
          {result.created} created · {result.ready} ready · {result.skipped} skipped
          · {result.errors} errors
        </p>
        <div class="overflow-auto max-h-72 rounded-md border border-border">
          <table class="w-full text-sm">
            <thead class="bg-muted">
              <tr>
                <th class="p-2 text-left">Row</th><th class="p-2 text-left">
                  Username
                </th><th class="p-2 text-left">Result</th>
              </tr>
            </thead>
            <tbody>
              {#each result.rows as row (row.row)}
                <tr class="border-t border-border">
                  <td class="p-2">{row.row}</td><td class="p-2">
                    {row.username}
                  </td><td
                    class="p-2"
                    class:text-destructive={row.status === "error"}
                  >
                    {row.message}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
      <div class="flex flex-wrap justify-end gap-2">
        <Button
          variant="secondary"
          disabled={busy}
          onclick={() => (open = false)}
        >
          Close
        </Button>
        <Button
          variant="secondary"
          disabled={!file || busy || completed}
          onclick={() => submit(true)}
        >
          {busy ? "Processing…" : "Preview CSV"}
        </Button>
        <Button
          disabled={busy ||
            !result ||
            result.errors > 0 ||
            result.ready === 0 ||
            completed}
          onclick={() => submit(false)}
        >
          Import {result?.ready ?? 0} Users
        </Button>
      </div>
    </div>
  </Dialog.Content>
</Dialog.Root>
