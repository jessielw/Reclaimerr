<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api, delete_api } from "$lib/api";
  import ErrorBox from "$lib/components/error-box.svelte";
  import Spinner from "$lib/components/ui/spinner.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import RequestStatusBadge from "$lib/components/requests/request-status-badge.svelte";
  import { Textarea } from "$lib/components/ui/textarea/index.js";
  import RequestsFilterBar from "$lib/components/requests/requests-filter-bar.svelte";
  import { toast } from "svelte-sonner";
  import { auth } from "$lib/stores/auth";
  import {
    type DeleteRequest,
    ProtectionRequestStatus,
    Permission,
  } from "$lib/types/shared";
  import { formatDate } from "$lib/utils/date";
  import { formatFileSize } from "$lib/utils/formatters";
  import CheckCircle from "@lucide/svelte/icons/check-circle";
  import XCircle from "@lucide/svelte/icons/x-circle";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import { cleanResolutionString } from "$lib/utils/formatters";

  type StatusFilter = "all" | ProtectionRequestStatus;
  type SortOrder = "desc" | "asc";

  const canManageRequests = $derived(
    $auth.user?.role === "admin" ||
      ($auth.user?.permissions ?? []).includes(Permission.ManageRequests),
  );

  let loading = $state(false);
  let error = $state<string | null>(null);
  let requests = $state<DeleteRequest[]>([]);
  let statusFilter = $state<StatusFilter>(ProtectionRequestStatus.Pending);
  let searchQuery = $state("");
  let sortOrder = $state<SortOrder>("desc");
  let selectedRequestId = $state<number | null>(null);

  let approveDialogOpen = $state(false);
  let approveTarget = $state<DeleteRequest | null>(null);
  let approveNotes = $state("");
  let approveSubmitting = $state(false);

  let denyDialogOpen = $state(false);
  let denyTarget = $state<DeleteRequest | null>(null);
  let denyNotes = $state("");
  let denySubmitting = $state(false);

  let cancelTarget = $state<DeleteRequest | null>(null);
  let cancelDialogOpen = $state(false);
  let cancelSubmitting = $state(false);

  const filteredRequests = $derived.by(() => {
    const normalizedSearch = searchQuery.trim().toLowerCase();
    let result =
      statusFilter === "all"
        ? [...requests]
        : requests.filter((r) => r.status === statusFilter);

    if (normalizedSearch) {
      result = result.filter((r) =>
        [r.media_title, r.reason, r.requested_by_username, r.execution_error]
          .filter(Boolean)
          .join(" ")
          .toLowerCase()
          .includes(normalizedSearch),
      );
    }

    result.sort((a, b) => {
      const aTime = new Date(a.created_at).getTime();
      const bTime = new Date(b.created_at).getTime();
      return sortOrder === "desc" ? bTime - aTime : aTime - bTime;
    });
    return result;
  });

  const selectedRequest = $derived(
    filteredRequests.find((r) => r.id === selectedRequestId) ?? null,
  );

  const loadRequests = async () => {
    loading = true;
    error = null;
    try {
      requests = canManageRequests
        ? await get_api<DeleteRequest[]>("/api/delete-requests")
        : await get_api<DeleteRequest[]>("/api/delete-requests/my");
    } catch (e: any) {
      error = e.message ?? "Failed to load delete requests.";
    } finally {
      loading = false;
    }
  };

  $effect(() => {
    if (filteredRequests.length === 0) {
      selectedRequestId = null;
      return;
    }
    if (!filteredRequests.some((request) => request.id === selectedRequestId)) {
      selectedRequestId = filteredRequests[0].id;
    }
  });

  const openApprove = (req: DeleteRequest) => {
    approveTarget = req;
    approveNotes = "";
    approveDialogOpen = true;
  };

  const openDeny = (req: DeleteRequest) => {
    denyTarget = req;
    denyNotes = "";
    denyDialogOpen = true;
  };

  const openCancel = (req: DeleteRequest) => {
    cancelTarget = req;
    cancelDialogOpen = true;
  };

  const submitApprove = async () => {
    if (!approveTarget) return;
    approveSubmitting = true;
    try {
      await post_api(`/api/delete-requests/${approveTarget.id}/approve`, {
        admin_notes: approveNotes.trim() || null,
      });
      toast.success("Delete request approved.");
      approveDialogOpen = false;
      await loadRequests();
    } catch (e: any) {
      toast.error(e.message ?? "Failed to approve request.");
    } finally {
      approveSubmitting = false;
    }
  };

  const submitDeny = async () => {
    if (!denyTarget) return;
    denySubmitting = true;
    try {
      await post_api(`/api/delete-requests/${denyTarget.id}/deny`, {
        admin_notes: denyNotes.trim() || null,
      });
      toast.success("Delete request denied.");
      denyDialogOpen = false;
      await loadRequests();
    } catch (e: any) {
      toast.error(e.message ?? "Failed to deny request.");
    } finally {
      denySubmitting = false;
    }
  };

  const submitCancel = async () => {
    if (!cancelTarget) return;
    cancelSubmitting = true;
    try {
      await delete_api(`/api/delete-requests/${cancelTarget.id}`);
      toast.success("Delete request cancelled.");
      cancelDialogOpen = false;
      await loadRequests();
    } catch (e: any) {
      toast.error(e.message ?? "Failed to cancel request.");
    } finally {
      cancelSubmitting = false;
    }
  };

  onMount(() => {
    loadRequests();
  });
</script>

<div class="px-2.5 md:px-8 pt-6 pb-2.5 md:pb-8">
  <div class="mx-auto max-w-7xl space-y-6">
    <RequestsFilterBar bind:searchQuery bind:statusFilter bind:sortOrder />

    {#if loading}
      <div class="flex items-center justify-center py-20">
        <Spinner class="size-10 text-primary" />
      </div>
    {:else if error}
      <ErrorBox {error} />
    {:else if filteredRequests.length === 0}
      <div class="rounded-xl border bg-card p-10 text-center">
        <p class="text-lg font-medium">No delete requests found</p>
      </div>
    {:else}
      <div class="grid gap-4 lg:grid-cols-[360px_minmax(0,1fr)]">
        <div class="overflow-hidden rounded-xl border bg-card">
          {#each filteredRequests as req (req.id)}
            <button
              class="w-full border-b p-4 text-left transition-colors hover:bg-muted/55 {selectedRequestId ===
              req.id
                ? 'bg-muted/65'
                : ''}"
              onclick={() => (selectedRequestId = req.id)}
            >
              <div class="flex items-start gap-3">
                <PosterThumb
                  mediaType={req.media_type}
                  posterUrl={req.poster_url}
                />
                <div class="min-w-0 flex-1">
                  <div class="flex items-start justify-between gap-2">
                    <div>
                      <p class="font-medium text-foreground">
                        {req.media_title}
                      </p>
                      <p class="text-sm text-muted-foreground">
                        Requested {formatDate(req.created_at)}
                      </p>
                    </div>
                    <RequestStatusBadge status={req.status} />
                  </div>
                  <div
                    class="mt-2 flex items-center gap-2 text-sm text-muted-foreground"
                  >
                    <MediaTypeBadge mediaType={req.media_type} />
                    <span>@{req.requested_by_username}</span>
                  </div>
                  {#if req.execution_error}
                    <p class="mt-2 line-clamp-2 text-sm text-red-500">
                      {req.execution_error}
                    </p>
                  {/if}
                </div>
              </div>
            </button>
          {/each}
        </div>

        <div class="rounded-xl border bg-card p-6">
          {#if selectedRequest}
            <div class="space-y-6">
              <div class="flex items-start justify-between gap-4">
                <div>
                  <h2 class="text-2xl font-semibold text-foreground">
                    {selectedRequest.media_title}
                  </h2>
                  <p class="text-sm text-muted-foreground">
                    Requested {formatDate(selectedRequest.created_at)} by @{selectedRequest.requested_by_username}
                  </p>
                </div>
                <RequestStatusBadge status={selectedRequest.status} />
              </div>

              <div class="grid gap-4 md:grid-cols-2">
                <div>
                  <p class="text-sm font-medium text-foreground">Target</p>
                  <p class="text-sm text-muted-foreground">
                    <MediaTypeBadge mediaType={selectedRequest.media_type} />
                    {#if selectedRequest.season_number != null}
                      <span class="ml-2"
                        >Season {selectedRequest.season_number}</span
                      >
                    {/if}
                    {#if selectedRequest.movie_version_id != null}
                      <span class="ml-2">Specific version</span>
                    {/if}
                  </p>
                </div>
                <div>
                  <p class="text-sm font-medium text-foreground">Execution</p>
                  <p class="text-sm text-muted-foreground">
                    {#if selectedRequest.executed_at}
                      Deleted {formatDate(selectedRequest.executed_at)}
                    {:else if selectedRequest.status === ProtectionRequestStatus.Approved}
                      Approved, not completed
                    {:else}
                      Not executed
                    {/if}
                  </p>
                </div>
              </div>

              {#if selectedRequest.movie_version_id != null || selectedRequest.season_id != null}
                <div class="text-foreground/75">
                  <p class="mb-2 text-sm font-medium text-foreground">
                    {selectedRequest.season_id != null
                      ? "Season Details"
                      : "Version Details"}
                  </p>
                  <div class="flex flex-wrap gap-1.5">
                    <!-- filename -->
                    {#if selectedRequest.version_file_name}
                      <span
                        class="break-all rounded-full border bg-muted px-2.5 py-0.5 font-mono text-xs"
                        title={selectedRequest.version_file_name}
                      >
                        {selectedRequest.version_file_name}
                      </span>
                    {/if}
                    <!-- resolution -->
                    {#if selectedRequest.version_resolution ?? selectedRequest.season_resolution}
                      <span
                        class="rounded-full border bg-muted px-2.5 py-0.5 text-xs font-medium"
                      >
                        {cleanResolutionString(
                          selectedRequest.version_resolution ??
                            selectedRequest.season_resolution,
                        )}
                      </span>
                    {/if}
                    <!-- video codec -->
                    {#if selectedRequest.version_video_codec}
                      <span
                        class="rounded-full border bg-muted px-2.5 py-0.5 text-xs font-medium"
                      >
                        {selectedRequest.version_video_codec.toUpperCase()}
                      </span>
                    {/if}
                    <!-- season video codecs -->
                    {#if selectedRequest.season_video_codecs?.length}
                      {#each selectedRequest.season_video_codecs as codec}
                        <span
                          class="rounded-full border bg-muted px-2.5 py-0.5 text-xs font-medium"
                        >
                          {codec.toUpperCase()}
                        </span>
                      {/each}
                    {/if}
                    <!-- HDR -->
                    {#if selectedRequest.version_hdr ?? selectedRequest.season_hdr}
                      <span
                        class="rounded-full border bg-muted px-2.5 py-0.5 text-xs font-medium"
                        >HDR</span
                      >
                    {/if}
                    <!-- dolby vision -->
                    {#if selectedRequest.version_dolby_vision ?? selectedRequest.season_dolby_vision}
                      <span
                        class="rounded-full border bg-muted px-2.5 py-0.5 text-xs font-medium"
                        >Dolby Vision</span
                      >
                    {/if}
                    <!-- file size -->
                    {#if selectedRequest.version_size ?? selectedRequest.season_size}
                      <span
                        class="rounded-full border bg-muted px-2.5 py-0.5 text-xs font-medium"
                      >
                        {formatFileSize(
                          selectedRequest.version_size ??
                            selectedRequest.season_size,
                        )}
                      </span>
                    {/if}
                  </div>
                </div>
              {/if}

              <div>
                <p class="mb-2 text-sm font-medium text-foreground">
                  Request Reason
                </p>
                <p
                  class="rounded-lg border bg-background/60 p-4 text-sm text-foreground"
                >
                  {selectedRequest.reason || "No reason provided"}
                </p>
              </div>

              {#if selectedRequest.admin_notes}
                <div>
                  <p class="mb-2 text-sm font-medium text-foreground">
                    Admin Notes
                  </p>
                  <p
                    class="rounded-lg border bg-background/60 p-4 text-sm text-foreground"
                  >
                    {selectedRequest.admin_notes}
                  </p>
                </div>
              {/if}

              {#if selectedRequest.execution_error}
                <div>
                  <p class="mb-2 text-sm font-medium text-red-500">
                    Execution Error
                  </p>
                  <p
                    class="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-200"
                  >
                    {selectedRequest.execution_error}
                  </p>
                </div>
              {/if}

              <div class="flex flex-wrap gap-3">
                {#if canManageRequests && selectedRequest.status === ProtectionRequestStatus.Pending}
                  <!-- approve delete request -->
                  <Button
                    class="cursor-pointer"
                    onclick={() => openApprove(selectedRequest)}
                  >
                    <CheckCircle class="size-4" /> Approve
                  </Button>

                  <!-- deny delete request -->
                  <Button
                    variant="secondary"
                    class="cursor-pointer"
                    onclick={() => openDeny(selectedRequest)}
                  >
                    <XCircle class="size-4" /> Deny
                  </Button>
                {/if}

                <!-- cancel delete request -->
                {#if selectedRequest.status === ProtectionRequestStatus.Pending && selectedRequest.requested_by_user_id === $auth.user?.id}
                  <Button
                    variant="destructive"
                    class="cursor-pointer"
                    onclick={() => openCancel(selectedRequest)}
                  >
                    <Trash2 class="size-4" /> Cancel Request
                  </Button>
                {/if}
              </div>
            </div>
          {:else}
            <p class="text-muted-foreground">
              Select a request to view details.
            </p>
          {/if}
        </div>
      </div>
    {/if}
  </div>
</div>

<!-- modals -->
<!-- approve delete request -->
<Dialog.Root bind:open={approveDialogOpen}>
  <Dialog.Content class="sm:max-w-xl text-foreground">
    <Dialog.Header>
      <Dialog.Title>Approve Delete Request</Dialog.Title>
      <Dialog.Description>
        {#if approveTarget}
          Approving deletion for <strong>{approveTarget.media_title}</strong> will
          execute the deletion immediately.
        {/if}
      </Dialog.Description>
    </Dialog.Header>
    <div class="space-y-3 py-4">
      <Textarea
        bind:value={approveNotes}
        placeholder="Admin notes (optional)"
        rows={4}
      />
    </div>
    <Dialog.Footer>
      <Button variant="secondary" onclick={() => (approveDialogOpen = false)}
        >Cancel</Button
      >
      <Button
        variant="destructive"
        onclick={submitApprove}
        disabled={approveSubmitting}
      >
        {approveSubmitting ? "Approving..." : "Approve and Delete"}
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

<!-- deny delete request -->
<Dialog.Root bind:open={denyDialogOpen}>
  <Dialog.Content class="sm:max-w-xl text-foreground">
    <Dialog.Header>
      <Dialog.Title>Deny Delete Request</Dialog.Title>
      <Dialog.Description>
        {#if denyTarget}
          Denying delete request for <strong>{denyTarget.media_title}</strong>.
        {/if}
      </Dialog.Description>
    </Dialog.Header>
    <div class="space-y-3 py-4">
      <Textarea
        bind:value={denyNotes}
        placeholder="Explain why this request is being denied..."
        rows={4}
      />
    </div>
    <Dialog.Footer>
      <Button
        variant="secondary"
        onclick={() => (denyDialogOpen = false)}
        class="cursor-pointer">Cancel</Button
      >
      <Button
        onclick={submitDeny}
        disabled={denySubmitting}
        class="cursor-pointer"
      >
        {denySubmitting ? "Denying..." : "Deny"}
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

<!-- cancel delete request -->
<Dialog.Root bind:open={cancelDialogOpen}>
  <Dialog.Content class="sm:max-w-lg text-foreground">
    <Dialog.Header>
      <Dialog.Title>Cancel Delete Request</Dialog.Title>
      <Dialog.Description>
        {#if cancelTarget}
          Are you sure you want to cancel your delete request for <strong
            >{cancelTarget.media_title}</strong
          >?
        {/if}
      </Dialog.Description>
    </Dialog.Header>
    <Dialog.Footer>
      <Button
        variant="secondary"
        onclick={() => (cancelDialogOpen = false)}
        class="cursor-pointer">Keep</Button
      >
      <Button
        variant="destructive"
        onclick={submitCancel}
        disabled={cancelSubmitting}
        class="cursor-pointer"
      >
        {cancelSubmitting ? "Cancelling..." : "Cancel Request"}
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
