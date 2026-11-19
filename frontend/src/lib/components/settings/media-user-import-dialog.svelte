<script lang="ts">
  import { tick } from "svelte";
  import { get_api, post_api } from "$lib/api";
  import { auth } from "$lib/stores/auth";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import ErrorBox from "$lib/components/error-box.svelte";
  import {
    DEFAULT_NEW_USER_ALLOWED_PAGES,
    PAGE_ACCESS_OPTIONS,
  } from "$lib/page-access";
  import { Permission, UserRole, type PageAccess } from "$lib/types/shared";
  import type {
    MediaImportProvider,
    MediaUserImportResponse,
    MediaUserImportRow,
  } from "$lib/types/media-user-import";

  let {
    open = $bindable(false),
    onImported,
  }: { open: boolean; onImported: () => Promise<void> } = $props();
  let dialog: HTMLDialogElement;
  let providers = $state<MediaImportProvider[]>([]);
  let providerId = $state("");
  let result = $state<MediaUserImportResponse | null>(null);
  let selected = $state<string[]>([]);
  let search = $state("");
  let busy = $state(false);
  let busyMessage = $state("Loading media servers…");
  let error = $state("");
  let refreshError = $state("");
  let identityLinkNotice = $state("");
  let reviewed = $state(false);
  let completed = $state(false);
  let reviewHeading: HTMLHeadingElement | undefined = $state();
  let selectionHeading: HTMLHeadingElement | undefined = $state();
  let resultsHeading: HTMLHeadingElement | undefined = $state();
  let role = $state<UserRole>(UserRole.User);
  let permissions = $state<Permission[]>([Permission.Request]);
  let pageMode = $state<"default" | "custom" | "all">("default");
  let pages = $state<PageAccess[]>([...DEFAULT_NEW_USER_ALLOWED_PAGES]);
  let generation = 0;
  const isAdmin = $derived($auth.user?.role === UserRole.Admin);
  const permissionOptions = [
    { value: Permission.Request, label: "Request" },
    { value: Permission.AutoApprove, label: "Auto approve" },
    { value: Permission.ManageRequests, label: "Manage requests" },
    { value: Permission.ManageProtection, label: "Manage protection" },
    { value: Permission.ManageReclaim, label: "Manage reclaim" },
    { value: Permission.ManageUsers, label: "Manage users" },
  ];
  const availablePermissions = $derived(
    permissionOptions.filter(
      (option) =>
        isAdmin || ($auth.user?.permissions ?? []).includes(option.value),
    ),
  );
  const availablePages = $derived(
    PAGE_ACCESS_OPTIONS.filter(
      (option) =>
        isAdmin ||
        $auth.user?.allowed_pages === null ||
        ($auth.user?.allowed_pages ?? []).includes(option.value),
    ),
  );
  const visibleRows = $derived(
    (result?.rows ?? []).filter((row) =>
      [row.username, row.email, row.local_username, row.source_user_id].some(
        (value) =>
          (value ?? "").toLowerCase().includes(search.trim().toLowerCase()),
      ),
    ),
  );
  const selectedRows = $derived(
    (result?.rows ?? []).filter((row) => selected.includes(row.source_user_id)),
  );
  const createCount = $derived(
    selectedRows.filter((row) => row.status === "ready_to_create").length,
  );
  const linkCount = $derived(
    selectedRows.filter(
      (row) => row.status === "ready_to_link" || row.status === "needs_review",
    ).length,
  );
  const selectedProviderName = $derived(
    providers.find(
      (provider) => String(provider.service_config_id) === providerId,
    )?.name ?? "Media server",
  );
  const permissionSummary = $derived(
    permissionOptions
      .filter((option) => permissions.includes(option.value))
      .map((option) => option.label)
      .join(", ") || "None",
  );
  const pageSummary = $derived(
    role === UserRole.Admin || pageMode === "all"
      ? "Unrestricted"
      : pageMode === "default"
        ? "Configured default pages"
        : PAGE_ACCESS_OPTIONS.filter((option) => pages.includes(option.value))
            .map((option) => option.label)
            .join(", ") || "No pages",
  );

  async function backToSelection() {
    reviewed = false;
    error = "";
    await tick();
    selectionHeading?.focus();
  }

  function invalidate() {
    result = null;
    selected = [];
    reviewed = false;
    completed = false;
    error = "";
    identityLinkNotice = "";
  }

  function selectable(row: MediaUserImportRow) {
    return ["ready_to_create", "ready_to_link", "needs_review"].includes(
      row.status,
    );
  }

  async function initialize() {
    const current = ++generation;
    invalidate();
    search = "";
    refreshError = "";
    providers = [];
    providerId = "";
    role = UserRole.User;
    permissions =
      isAdmin || ($auth.user?.permissions ?? []).includes(Permission.Request)
        ? [Permission.Request]
        : [];
    pageMode = "default";
    pages = DEFAULT_NEW_USER_ALLOWED_PAGES.filter((page) =>
      availablePages.some((option) => option.value === page),
    );
    busy = true;
    busyMessage = "Loading media servers…";
    try {
      const items = await get_api<MediaImportProvider[]>(
        "/api/account/users/import-from-media-server/providers",
      );
      if (current !== generation || !open) return;
      providers = items;
      providerId = items[0] ? String(items[0].service_config_id) : "";
    } catch (err) {
      if (current === generation && open)
        error =
          err instanceof Error ? err.message : "Could not load media servers";
    } finally {
      if (current === generation) busy = false;
    }
  }

  $effect(() => {
    if (open && dialog && !dialog.open) {
      dialog.showModal();
      void initialize();
    } else if (!open && dialog?.open) {
      generation++;
      dialog.close();
    }
  });

  async function submit(mode: "discover" | "review" | "import") {
    if (busy || completed) return;
    if (!providerId) {
      error = "Select an enabled media server first.";
      return;
    }
    if (mode !== "discover" && selected.length === 0) {
      error = "Select at least one user to import or link.";
      return;
    }
    const current = generation;
    const rows = result?.rows ?? [];
    const links =
      mode === "discover"
        ? []
        : selectedRows
            .filter((row) => row.account)
            .map((row) => ({
              source_user_id: row.source_user_id,
              target_user_id: row.account!.id,
            }));
    busy = true;
    busyMessage =
      mode === "import"
        ? "Importing selected users…"
        : mode === "review"
          ? "Checking selected users before confirmation…"
          : "Loading users from the media server…";
    error = "";
    try {
      const response = await post_api<MediaUserImportResponse>(
        "/api/account/users/import-from-media-server?preview=" +
          (mode !== "import"),
        {
          service_config_id: Number(providerId),
          source_user_ids: mode === "discover" ? null : selected,
          links,
          role,
          permissions,
          use_default_page_access: pageMode === "default",
          allowed_pages: pageMode === "all" ? null : pages,
          expected_revisions:
            mode === "import"
              ? Object.fromEntries(
                  rows
                    .filter((row) => selected.includes(row.source_user_id))
                    .map((row) => [row.source_user_id, row.revision]),
                )
              : {},
        },
      );
      if (current !== generation || !open) return;
      result = response;
      reviewed = mode === "review";
      completed = mode === "import";
      if (
        mode === "review" &&
        response.rows.some(
          (row) => selected.includes(row.source_user_id) && !selectable(row),
        )
      ) {
        reviewed = false;
        selected = selected.filter((id) =>
          response.rows.some(
            (row) => row.source_user_id === id && selectable(row),
          ),
        );
        error =
          "Some selected users are no longer importable. Review the results before continuing.";
      }
      if (mode === "discover")
        selected = response.rows
          .filter((row) => row.status === "ready_to_create")
          .map((row) => row.source_user_id);
      if (reviewed || completed) {
        await tick();
        if (current === generation && open) {
          (completed ? resultsHeading : reviewHeading)?.focus();
          dialog.scrollTop = 0;
        }
      }
      if (mode === "import" && response.created + response.linked > 0) {
        try {
          await onImported();
        } catch {
          refreshError =
            "Import succeeded. Close this dialog and refresh the Users page to update the list.";
        }
      }
    } catch (err) {
      if (current !== generation || !open) return;
      error = err instanceof Error ? err.message : "Import failed";
      reviewed = false;
    } finally {
      if (current === generation) busy = false;
    }
  }

  async function stageIdentity(row: MediaUserImportRow) {
    if (busy) return;
    busy = true;
    busyMessage = "Preparing the manual identity link…";
    error = "";
    identityLinkNotice = "";
    try {
      await post_api("/api/account/users/media-identities/discover", {
        service_config_id: Number(providerId),
        source_user_id: row.source_user_id,
      });
      await onImported();
      identityLinkNotice = `${row.username} is ready in Sign-In Identity Links. Close this dialog and select its local account there, then refresh the preview.`;
    } catch (err) {
      error =
        err instanceof Error
          ? err.message
          : "Could not prepare the identity link";
    } finally {
      busy = false;
    }
  }
</script>

<dialog
  bind:this={dialog}
  aria-labelledby="media-import-title"
  aria-describedby="media-import-description"
  class="m-auto w-[calc(100%-2rem)] max-w-4xl max-h-[90dvh] overflow-y-auto rounded-lg border border-border bg-card p-6 text-card-foreground shadow-lg"
  onclose={() => {
    open = false;
    generation++;
  }}
  oncancel={(event) => {
    if (busy) event.preventDefault();
  }}
>
  <h2 id="media-import-title" class="text-lg font-semibold">
    Import from media server
  </h2>
  <p id="media-import-description" class="mt-1 text-sm text-muted-foreground">
    {completed
      ? "Import complete. Review the results below."
      : reviewed
        ? "Confirm the selected account changes below. No accounts have been changed yet."
        : "Choose users to create or link, then review their account changes before importing."}
  </p>
  <ol
    aria-label="Import progress"
    class="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-sm"
  >
    <li
      aria-current={!reviewed && !completed ? "step" : undefined}
      class:font-semibold={!reviewed && !completed}
      class:text-muted-foreground={reviewed || completed}
    >
      1. Select users
    </li>
    <li
      aria-current={reviewed && !completed ? "step" : undefined}
      class:font-semibold={reviewed && !completed}
      class:text-muted-foreground={!reviewed || completed}
    >
      2. Confirm import
    </li>
    <li
      aria-current={completed ? "step" : undefined}
      class:font-semibold={completed}
      class:text-muted-foreground={!completed}
    >
      3. Results
    </li>
  </ol>
  <form
    class="mt-4 space-y-4"
    onsubmit={(event) => {
      event.preventDefault();
      void submit(result ? (reviewed ? "import" : "review") : "discover");
    }}
  >
    {#if !reviewed && !completed}
      <h3
        bind:this={selectionHeading}
        tabindex="-1"
        class="text-base font-semibold"
      >
        Select users
      </h3>
      <fieldset disabled={busy} class="space-y-4">
        <legend class="sr-only">Media server and new account access</legend>
        <div class="space-y-2">
          <Label for="media-import-provider">Media server</Label>
          <select
            id="media-import-provider"
            name="service_config_id"
            bind:value={providerId}
            onchange={invalidate}
            class="w-full rounded-md border border-input bg-background p-2 text-base"
          >
            <option value="">Select a server</option>
            {#each providers as provider (provider.service_config_id)}
              <option value={String(provider.service_config_id)}
                >{provider.name} ({provider.service_type})</option
              >
            {/each}
          </select>
          {#if !busy && providers.length === 0}<p
              class="text-sm text-muted-foreground"
            >
              No enabled media servers are available. Configure one under
              Settings → Servers.
            </p>{/if}
        </div>
        <details class="rounded-md border border-border p-3">
          <summary class="cursor-pointer text-sm font-medium"
            >Access for new accounts</summary
          >
          <div class="mt-3 space-y-4">
            {#if isAdmin}
              <fieldset class="flex flex-wrap gap-4">
                <legend class="mb-2 text-sm font-medium">Role</legend>
                <label
                  for="media-import-role-user"
                  class="flex items-center gap-2"
                  ><input
                    id="media-import-role-user"
                    type="radio"
                    name="role"
                    value={UserRole.User}
                    bind:group={role}
                    onchange={invalidate}
                  /> User</label
                >
                <label
                  for="media-import-role-admin"
                  class="flex items-center gap-2"
                  ><input
                    id="media-import-role-admin"
                    type="radio"
                    name="role"
                    value={UserRole.Admin}
                    bind:group={role}
                    onchange={invalidate}
                  /> Admin</label
                >
              </fieldset>
            {/if}
            {#if role === UserRole.User}
              <fieldset class="flex flex-wrap gap-4">
                <legend class="mb-2 text-sm font-medium">Permissions</legend>
                {#each availablePermissions as option (option.value)}
                  <label
                    for={`media-import-permission-${option.value}`}
                    class="flex items-center gap-2"
                    ><input
                      id={`media-import-permission-${option.value}`}
                      type="checkbox"
                      name="permissions"
                      value={option.value}
                      bind:group={permissions}
                      onchange={invalidate}
                    />
                    {option.label}</label
                  >
                {/each}
              </fieldset>
              <fieldset class="space-y-2">
                <legend class="mb-2 text-sm font-medium">Page access</legend>
                <label
                  for="media-import-pages-default"
                  class="flex items-center gap-2"
                  ><input
                    id="media-import-pages-default"
                    type="radio"
                    name="page_access"
                    value="default"
                    bind:group={pageMode}
                    onchange={invalidate}
                  /> Configured defaults</label
                >
                <label
                  for="media-import-pages-custom"
                  class="flex items-center gap-2"
                  ><input
                    id="media-import-pages-custom"
                    type="radio"
                    name="page_access"
                    value="custom"
                    bind:group={pageMode}
                    onchange={invalidate}
                  /> Choose pages</label
                >
                {#if isAdmin || $auth.user?.allowed_pages === null}
                  <label
                    for="media-import-pages-all"
                    class="flex items-center gap-2"
                    ><input
                      id="media-import-pages-all"
                      type="radio"
                      name="page_access"
                      value="all"
                      bind:group={pageMode}
                      onchange={invalidate}
                    /> Unrestricted</label
                  >
                {/if}
                {#if pageMode === "custom"}
                  <div class="flex flex-wrap gap-4 pt-2">
                    {#each availablePages as option (option.value)}
                      <label
                        for={`media-import-page-${option.value}`}
                        class="flex items-center gap-2"
                        ><input
                          id={`media-import-page-${option.value}`}
                          type="checkbox"
                          name="allowed_pages"
                          value={option.value}
                          bind:group={pages}
                          onchange={invalidate}
                        />
                        {option.label}</label
                      >
                    {/each}
                  </div>
                {/if}
              </fieldset>
            {:else}<p class="text-sm">
                Admin accounts have unrestricted access.
              </p>{/if}
          </div>
        </details>
      </fieldset>
      <p class="text-sm text-muted-foreground">
        New accounts have no local password. Enable media sign-in to let them
        log in through their server. Trusted proxy sign-in requires the
        displayed local username. Existing accounts keep their settings.
      </p>
    {/if}
    <div role="alert" aria-atomic="true"><ErrorBox {error} /></div>
    {#if identityLinkNotice}<p role="status" class="text-sm">
        {identityLinkNotice}
      </p>{/if}
    {#if refreshError}<p role="status" class="text-sm">{refreshError}</p>{/if}
    {#if busy}<p class="text-sm">
        {busyMessage}
      </p>{/if}
    {#if reviewed && !completed}
      <section aria-labelledby="media-import-review-heading" class="space-y-4">
        <h3
          id="media-import-review-heading"
          bind:this={reviewHeading}
          tabindex="-1"
          class="text-base font-semibold"
        >
          Confirm import from {selectedProviderName}
        </h3>
        <div class="grid gap-3 sm:grid-cols-2">
          <div class="rounded-md border border-border bg-muted/40 p-4">
            <p class="text-2xl font-semibold">{createCount}</p>
            <p class="text-sm text-muted-foreground">New accounts to create</p>
          </div>
          <div class="rounded-md border border-border bg-muted/40 p-4">
            <p class="text-2xl font-semibold">{linkCount}</p>
            <p class="text-sm text-muted-foreground">
              Existing accounts to link
            </p>
          </div>
        </div>
        <div class="max-h-80 overflow-auto rounded-md border border-border">
          <table class="w-full text-left text-sm">
            <caption class="sr-only"
              >Selected users and the account changes to confirm</caption
            >
            <thead class="bg-muted"
              ><tr
                ><th scope="col" class="p-3">Media user</th><th
                  scope="col"
                  class="p-3">Reclaimerr account</th
                ><th scope="col" class="p-3">Action</th></tr
              ></thead
            >
            <tbody>
              {#each selectedRows as row (row.source_user_id)}
                <tr class="border-t border-border"
                  ><td class="p-3 break-all"
                    >{row.username}{#if row.email}<div
                        class="text-muted-foreground"
                      >
                        {row.email}
                      </div>{/if}</td
                  ><td class="p-3 break-all"
                    >@{row.local_username}{#if row.account && !row.account.is_active}<div
                        class="text-muted-foreground"
                      >
                        Account remains disabled
                      </div>{/if}</td
                  ><td class="p-3"
                    >{row.status === "ready_to_create"
                      ? "Create account"
                      : "Link existing account"}</td
                  ></tr
                >
              {/each}
            </tbody>
          </table>
        </div>
        {#if createCount > 0}
          <section
            aria-labelledby="media-import-access-heading"
            class="rounded-md border border-border p-4"
          >
            <h4 id="media-import-access-heading" class="mb-3 font-medium">
              Access for new accounts
            </h4>
            <dl class="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[auto_1fr]">
              <dt class="text-muted-foreground">Role</dt>
              <dd>{role === UserRole.Admin ? "Admin" : "User"}</dd>
              <dt class="text-muted-foreground">Permissions</dt>
              <dd>
                {role === UserRole.Admin
                  ? "All permissions"
                  : permissionSummary}
              </dd>
              <dt class="text-muted-foreground">Pages</dt>
              <dd>{pageSummary}</dd>
              <dt class="text-muted-foreground">Sign-in</dt>
              <dd>Media server or trusted proxy; no local password</dd>
            </dl>
          </section>
        {/if}
        {#if linkCount > 0}<p class="text-sm text-muted-foreground">
            Existing accounts keep their passwords, permissions, page access,
            and enabled or disabled state.
          </p>{/if}
        <p class="text-sm text-muted-foreground">
          Select <strong>Confirm import</strong> to apply these changes. If any selected
          user fails, none of the selected changes are applied.
        </p>
      </section>
    {:else if result}
      {#if completed}<h3
          bind:this={resultsHeading}
          tabindex="-1"
          class="text-base font-semibold"
        >
          Import results
        </h3>{/if}
      <div class="space-y-2">
        <Label for="media-import-search">Search media users</Label>
        <Input
          id="media-import-search"
          type="search"
          name="search"
          bind:value={search}
          autocomplete="off"
          spellcheck={false}
        />
      </div>
      {#if completed}
        <p role="status" class="text-sm font-medium">
          {result.created} created · {result.linked} linked · {result.skipped} skipped
        </p>
      {:else}
        <p role="status" class="text-sm">
          {selected.length} selected: {createCount} new accounts · {linkCount} existing
          account links
        </p>
        <p class="text-sm text-muted-foreground">
          Checking a suggested link explicitly confirms that local account.
          Conflicts must be resolved in Sign-In Identity Links.
        </p>
        <div class="flex flex-wrap gap-2">
          <Button
            variant="secondary"
            size="sm"
            disabled={busy}
            onclick={() => {
              selected = result!.rows
                .filter((row) => row.status === "ready_to_create")
                .map((row) => row.source_user_id);
              reviewed = false;
            }}>Select new accounts</Button
          >
          <Button
            variant="secondary"
            size="sm"
            disabled={busy}
            onclick={() => {
              selected = [];
              reviewed = false;
            }}>Clear selection</Button
          >
        </div>
      {/if}
      <div class="max-h-80 overflow-auto rounded-md border border-border">
        <table class="w-full text-left text-sm">
          <caption class="sr-only"
            >Media users and proposed account changes</caption
          >
          <thead class="bg-muted"
            ><tr
              >{#if !completed}<th scope="col" class="p-3">Select</th>{/if}<th
                scope="col"
                class="p-3">Media user</th
              ><th scope="col" class="p-3">Local account</th><th
                scope="col"
                class="p-3">Result</th
              ></tr
            ></thead
          >
          <tbody>
            {#each visibleRows as row (row.source_user_id)}
              <tr class="border-t border-border">
                {#if !completed}<td class="p-3"
                    ><input
                      type="checkbox"
                      name="source_user_ids"
                      aria-label={`Select ${row.username}${row.account ? ` to link @${row.account.username}` : " to create an account"}`}
                      value={row.source_user_id}
                      bind:group={selected}
                      disabled={busy || completed || !selectable(row)}
                      onchange={() => {
                        reviewed = false;
                      }}
                    /></td
                  >{/if}
                <td class="p-3 break-all"
                  >{row.username}{#if row.email}<div
                      class="text-muted-foreground"
                    >
                      {row.email}
                    </div>{/if}
                  <div class="text-xs text-muted-foreground">
                    ID: {row.source_user_id}
                  </div></td
                >
                <td class="p-3 break-all"
                  >{row.local_username
                    ? `@${row.local_username}`
                    : "—"}{#if row.account && !row.account.is_active}<div>
                      Disabled account
                    </div>{/if}</td
                >
                <td class="p-3"
                  >{row.message}
                  {#if row.status === "conflict" && !completed}
                    <Button
                      size="sm"
                      variant="secondary"
                      class="mt-2"
                      disabled={busy}
                      onclick={() => stageIdentity(row)}
                      >Add to identity links</Button
                    >
                  {/if}
                </td>
              </tr>
            {:else}<tr
                ><td colspan={completed ? 3 : 4} class="p-3"
                  >No media users match.</td
                ></tr
              >{/each}
          </tbody>
        </table>
      </div>
    {/if}
    <div class="flex flex-wrap justify-end gap-2">
      <Button variant="secondary" disabled={busy} onclick={() => dialog.close()}
        >Close</Button
      >
      {#if reviewed && !completed}<Button
          variant="secondary"
          disabled={busy}
          onclick={backToSelection}>Back to selection</Button
        >
      {:else if result && !completed}<Button
          variant="secondary"
          disabled={busy}
          onclick={() => submit("discover")}>Refresh preview</Button
        >{/if}
      {#if !completed}<Button type="submit" disabled={busy}
          >{!result
            ? "Preview users"
            : reviewed
              ? "Confirm import"
              : "Review selection"}</Button
        >{/if}
    </div>
  </form>
</dialog>

<style>
  dialog:modal {
    overscroll-behavior: contain;
  }
  dialog::backdrop {
    background: rgb(0 0 0 / 60%);
    overflow: hidden;
    overscroll-behavior: contain;
  }
  input[type="checkbox"],
  input[type="radio"] {
    accent-color: var(--primary);
    min-width: 1rem;
    min-height: 1rem;
  }
  input:focus-visible,
  select:focus-visible,
  summary:focus-visible {
    outline: 2px solid var(--ring);
    outline-offset: 3px;
  }
</style>
