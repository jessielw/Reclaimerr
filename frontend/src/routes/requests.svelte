<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api, delete_api } from "$lib/api";
  import ErrorBox from "$lib/components/ErrorBox.svelte";
  import Spinner from "$lib/components/ui/spinner.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import MediaTypeBadge from "$lib/components/requests/MediaTypeBadge.svelte";
  import PosterThumb from "$lib/components/requests/PosterThumb.svelte";
  import RequestStatusBadge from "$lib/components/requests/RequestStatusBadge.svelte";
  import { Input } from "$lib/components/ui/input/index.js";
  import { toast } from "svelte-sonner";
  import { auth } from "$lib/stores/auth";
  import {
    type ExceptionRequest,
    ExceptionRequestStatus,
    Permission,
  } from "$lib/types/shared";
  import { formatDate } from "$lib/utils/date";
  import CheckCircle from "@lucide/svelte/icons/check-circle";
  import XCircle from "@lucide/svelte/icons/x-circle";
  import Trash2 from "@lucide/svelte/icons/trash-2";

  const TMDB_POSTER_WIDTH = 92;

  type StatusFilter = "all" | ExceptionRequestStatus;
  type SortOrder = "desc" | "asc";

  const canManageRequests = $derived(
    $auth.user?.role === "admin" ||
      ($auth.user?.permissions ?? []).includes(Permission.ManageRequests),
  );

  let loading = $state(false);
  let error = $state<string | null>(null);
  let requests = $state<ExceptionRequest[]>([]);
  let statusFilter = $state<StatusFilter>(ExceptionRequestStatus.Pending);
  let searchQuery = $state("");
  let sortOrder = $state<SortOrder>("desc");
  let selectedRequestId = $state<number | null>(null);
  let selectedIds = $state<number[]>([]);

  // approve dialog state
  let approveDialogOpen = $state(false);
  let approveTarget = $state<ExceptionRequest | null>(null);
  let approveNotes = $state("");
  let approveDuration = $state("user_requested");
  let approveCustomDays = $state("30");
  let approveSubmitting = $state(false);

  // deny dialog state
  let denyDialogOpen = $state(false);
  let denyTarget = $state<ExceptionRequest | null>(null);
  let denyNotes = $state("");
  let denySubmitting = $state(false);

  // cancel confirmation state
  let cancelTarget = $state<ExceptionRequest | null>(null);
  let cancelDialogOpen = $state(false);
  let cancelSubmitting = $state(false);

  // bulk action state
  let bulkApproveDialogOpen = $state(false);
  let bulkDenyDialogOpen = $state(false);
  let bulkNotes = $state("");
  let bulkDuration = $state("user_requested");
  let bulkCustomDays = $state("30");
  let bulkSubmitting = $state(false);

  const filteredRequests = $derived.by(() => {
    const normalizedSearch = searchQuery.trim().toLowerCase();

    let result =
      statusFilter === "all"
        ? [...requests]
        : requests.filter((r) => r.status === statusFilter);

    if (normalizedSearch) {
      result = result.filter((r) =>
        [r.media_title, r.reason, r.requested_by_username]
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

  // const pendingCount = $derived(
  //   requests.filter((r) => r.status === ExceptionRequestStatus.Pending).length,
  // );

  const selectedRequest = $derived(
    filteredRequests.find((r) => r.id === selectedRequestId) ?? null,
  );

  const pendingInView = $derived(
    filteredRequests.filter((r) => r.status === ExceptionRequestStatus.Pending),
  );

  const selectedPendingIds = $derived(
    selectedIds.filter((id) =>
      pendingInView.some((request) => request.id === id),
    ),
  );

  const allPendingSelected = $derived(
    pendingInView.length > 0 &&
      selectedPendingIds.length === pendingInView.length,
  );

  // load requests from API
  const loadRequests = async () => {
    loading = true;
    error = null;
    try {
      if (canManageRequests) {
        requests = await get_api<ExceptionRequest[]>("/api/requests");
      } else {
        requests = await get_api<ExceptionRequest[]>("/api/requests/my");
      }
    } catch (e: any) {
      error = e.message ?? "Failed to load requests.";
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

  // watch for changes in statusFilter, searchQuery, or sortOrder to reset selection
  const openApprove = (req: ExceptionRequest) => {
    approveTarget = req;
    approveNotes = "";
    approveDuration = "user_requested";
    approveCustomDays = "30";
    approveDialogOpen = true;
  };

  const openDeny = (req: ExceptionRequest) => {
    denyTarget = req;
    denyNotes = "";
    denyDialogOpen = true;
  };

  const openCancel = (req: ExceptionRequest) => {
    cancelTarget = req;
    cancelDialogOpen = true;
  };

  const toggleSelection = (requestId: number) => {
    if (selectedIds.includes(requestId)) {
      selectedIds = selectedIds.filter((id) => id !== requestId);
      return;
    }
    selectedIds = [...selectedIds, requestId];
  };

  const toggleSelectAllPending = () => {
    if (allPendingSelected) {
      selectedIds = selectedIds.filter(
        (id) => !pendingInView.some((request) => request.id === id),
      );
      return;
    }
    const merged = new Set([...selectedIds, ...pendingInView.map((r) => r.id)]);
    selectedIds = [...merged];
  };

  const openBulkApprove = () => {
    bulkNotes = "";
    bulkDuration = "user_requested";
    bulkCustomDays = "30";
    bulkApproveDialogOpen = true;
  };

  const openBulkDeny = () => {
    bulkNotes = "";
    bulkDenyDialogOpen = true;
  };

  const submitApprove = async () => {
    if (!approveTarget) return;
    approveSubmitting = true;
    try {
      let approved_duration_days: number | null = null;
      let approved_permanent: boolean | null = null;

      if (approveDuration === "user_requested") {
        // leave both null — backend uses the user's original requested_expires_at
      } else if (approveDuration === "forever") {
        approved_permanent = true;
      } else if (approveDuration === "custom") {
        const customDays = Number(approveCustomDays);
        if (!Number.isInteger(customDays) || customDays <= 0) {
          toast.error(
            "Custom duration must be a positive whole number of days.",
          );
          approveSubmitting = false;
          return;
        }
        approved_duration_days = customDays;
      } else {
        approved_duration_days = Number(approveDuration);
      }

      await post_api(`/api/requests/${approveTarget.id}/approve`, {
        admin_notes: approveNotes || null,
        approved_duration_days,
        approved_permanent,
      });
      toast.success(`Request for "${approveTarget.media_title}" approved.`);
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
      await post_api(`/api/requests/${denyTarget.id}/deny`, {
        admin_notes: denyNotes || null,
      });
      toast.success(`Request for "${denyTarget.media_title}" denied.`);
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
      await delete_api(`/api/requests/${cancelTarget.id}`);
      toast.success(`Request for "${cancelTarget.media_title}" cancelled.`);
      cancelDialogOpen = false;
      await loadRequests();
    } catch (e: any) {
      toast.error(e.message ?? "Failed to cancel request.");
    } finally {
      cancelSubmitting = false;
    }
  };

  const submitBulkApprove = async () => {
    if (selectedPendingIds.length === 0) return;
    bulkSubmitting = true;

    try {
      let approved_duration_days: number | null = null;
      let approved_permanent: boolean | null = null;

      if (bulkDuration === "user_requested") {
        // leave as null and backend keeps each request's requested duration
      } else if (bulkDuration === "forever") {
        approved_permanent = true;
      } else if (bulkDuration === "custom") {
        const customDays = Number(bulkCustomDays);
        if (!Number.isInteger(customDays) || customDays <= 0) {
          toast.error(
            "Custom duration must be a positive whole number of days.",
          );
          bulkSubmitting = false;
          return;
        }
        approved_duration_days = customDays;
      } else {
        approved_duration_days = Number(bulkDuration);
      }

      let failed = 0;
      for (const requestId of selectedPendingIds) {
        try {
          await post_api(`/api/requests/${requestId}/approve`, {
            admin_notes: bulkNotes || null,
            approved_duration_days,
            approved_permanent,
          });
        } catch {
          failed += 1;
        }
      }

      const successCount = selectedPendingIds.length - failed;
      if (successCount > 0) {
        toast.success(
          `Approved ${successCount} request${successCount === 1 ? "" : "s"}.`,
        );
      }
      if (failed > 0) {
        toast.warning(
          `Failed to approve ${failed} request${failed === 1 ? "" : "s"}.`,
        );
      }

      bulkApproveDialogOpen = false;
      selectedIds = [];
      await loadRequests();
    } finally {
      bulkSubmitting = false;
    }
  };

  const submitBulkDeny = async () => {
    if (selectedPendingIds.length === 0) return;
    bulkSubmitting = true;

    try {
      let failed = 0;
      for (const requestId of selectedPendingIds) {
        try {
          await post_api(`/api/requests/${requestId}/deny`, {
            admin_notes: bulkNotes || null,
          });
        } catch {
          failed += 1;
        }
      }

      const successCount = selectedPendingIds.length - failed;
      if (successCount > 0) {
        toast.success(
          `Denied ${successCount} request${successCount === 1 ? "" : "s"}.`,
        );
      }
      if (failed > 0) {
        toast.warning(
          `Failed to deny ${failed} request${failed === 1 ? "" : "s"}.`,
        );
      }

      bulkDenyDialogOpen = false;
      selectedIds = [];
      await loadRequests();
    } finally {
      bulkSubmitting = false;
    }
  };

  const formatProtectionLabel = (req: ExceptionRequest): string => {
    if (!req.requested_expires_at) return "Forever";
    return `Until ${formatDate(req.requested_expires_at)}`;
  };

  const formatEffectiveProtectionLabel = (req: ExceptionRequest): string => {
    if (req.status !== ExceptionRequestStatus.Approved) {
      return formatProtectionLabel(req);
    }

    if (req.effective_permanent === true) return "Forever";
    if (req.effective_expires_at)
      return `Until ${formatDate(req.effective_expires_at)}`;

    return formatProtectionLabel(req);
  };

  const hasProtectionOverride = (req: ExceptionRequest): boolean => {
    return (
      req.status === ExceptionRequestStatus.Approved &&
      formatEffectiveProtectionLabel(req) !== formatProtectionLabel(req)
    );
  };

  onMount(() => {
    loadRequests();
  });
</script>

<!-- approve dialog -->
<Dialog.Root bind:open={approveDialogOpen}>
  <Dialog.Content class="sm:max-w-md text-foreground">
    <Dialog.Header>
      <Dialog.Title>Approve Request</Dialog.Title>
      <Dialog.Description>
        {#if approveTarget}
          Approving exception for <strong>{approveTarget.media_title}</strong>.
        {/if}
      </Dialog.Description>
    </Dialog.Header>
    <div class="flex flex-col gap-4 py-2">
      <div class="flex flex-col gap-2">
        <Label for="approve-duration">Protection Duration</Label>
        <Select.Root type="single" bind:value={approveDuration}>
          <Select.Trigger
            class="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 
            text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 
            focus-visible:ring-ring"
          >
            {(() => {
              if (approveDuration === "user_requested")
                return `Use requested (${approveTarget ? formatProtectionLabel(approveTarget) : "—"})`;
              if (approveDuration === "forever") return "Forever";
              if (approveDuration === "custom") return "Custom days";
              if (!isNaN(Number(approveDuration)))
                return `${approveDuration} days`;
              return approveDuration;
            })()}
          </Select.Trigger>
          <Select.Content class="bg-card border-ring">
            <Select.Item
              value="user_requested"
              label={approveTarget
                ? `Use requested (${formatProtectionLabel(approveTarget)})`
                : "Use requested (—)"}
              class="text-foreground"
              >{approveTarget
                ? `Use requested (${formatProtectionLabel(approveTarget)})`
                : "Use requested (—)"}</Select.Item
            >
            <Select.Item value="30" label="30 days" class="text-foreground"
              >30 days</Select.Item
            >
            <Select.Item value="90" label="90 days" class="text-foreground"
              >90 days</Select.Item
            >
            <Select.Item value="180" label="180 days" class="text-foreground"
              >180 days</Select.Item
            >
            <Select.Item value="365" label="1 year" class="text-foreground"
              >1 year</Select.Item
            >
            <Select.Item
              value="custom"
              label="Custom days"
              class="text-foreground">Custom days</Select.Item
            >
            <Select.Item value="forever" label="Forever" class="text-foreground"
              >Forever</Select.Item
            >
          </Select.Content>
        </Select.Root>
        {#if approveDuration === "custom"}
          <input
            type="number"
            min="1"
            step="1"
            bind:value={approveCustomDays}
            placeholder="Enter number of days"
            class="h-9 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm
              placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1
              focus-visible:ring-ring"
          />
        {/if}
      </div>
      <div class="flex flex-col gap-2">
        <Label for="approve-notes">Admin Notes (optional)</Label>
        <textarea
          id="approve-notes"
          bind:value={approveNotes}
          placeholder="Leave a note for the requester…"
          rows={3}
          class="flex w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm
            placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1
            focus-visible:ring-ring resize-none"
        ></textarea>
      </div>
    </div>
    <Dialog.Footer>
      <Button variant="outline" onclick={() => (approveDialogOpen = false)}>
        Cancel
      </Button>
      <Button
        variant="default"
        onclick={submitApprove}
        disabled={approveSubmitting}
        class="bg-green-600 hover:bg-green-700 text-white"
      >
        {#if approveSubmitting}
          <Spinner size="sm" class="mr-2" />
        {/if}
        Approve
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

<!-- deny dialog -->
<Dialog.Root bind:open={denyDialogOpen}>
  <Dialog.Content class="sm:max-w-md text-foreground">
    <Dialog.Header>
      <Dialog.Title>Deny Request</Dialog.Title>
      <Dialog.Description>
        {#if denyTarget}
          Denying exception request for <strong>{denyTarget.media_title}</strong
          >.
        {/if}
      </Dialog.Description>
    </Dialog.Header>
    <div class="flex flex-col gap-4 py-2">
      <div class="flex flex-col gap-2">
        <Label for="deny-notes">Reason / Admin Notes (optional)</Label>
        <textarea
          id="deny-notes"
          bind:value={denyNotes}
          placeholder="Explain why this request is being denied…"
          rows={3}
          class="flex w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm
            placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1
            focus-visible:ring-ring resize-none"
        ></textarea>
      </div>
    </div>
    <Dialog.Footer>
      <Button variant="outline" onclick={() => (denyDialogOpen = false)}>
        Cancel
      </Button>
      <Button
        variant="destructive"
        onclick={submitDeny}
        disabled={denySubmitting}
      >
        {#if denySubmitting}
          <Spinner size="sm" class="mr-2" />
        {/if}
        Deny
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

<!-- cancel confirmation dialog -->
<Dialog.Root bind:open={cancelDialogOpen}>
  <Dialog.Content class="sm:max-w-sm text-foreground">
    <Dialog.Header>
      <Dialog.Title>Cancel Request</Dialog.Title>
      <Dialog.Description>
        {#if cancelTarget}
          Are you sure you want to cancel your request for <strong
            >{cancelTarget.media_title}</strong
          >? This cannot be undone.
        {/if}
      </Dialog.Description>
    </Dialog.Header>
    <Dialog.Footer>
      <Button variant="outline" onclick={() => (cancelDialogOpen = false)}>
        Back
      </Button>
      <Button
        variant="destructive"
        onclick={submitCancel}
        disabled={cancelSubmitting}
      >
        {#if cancelSubmitting}
          <Spinner size="sm" class="mr-2" />
        {/if}
        Cancel Request
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

<!-- bulk approve dialog -->
<Dialog.Root bind:open={bulkApproveDialogOpen}>
  <Dialog.Content class="sm:max-w-md">
    <Dialog.Header>
      <Dialog.Title>Approve Selected</Dialog.Title>
      <Dialog.Description>
        Approve {selectedPendingIds.length} pending request{selectedPendingIds.length ===
        1
          ? ""
          : "s"}.
      </Dialog.Description>
    </Dialog.Header>
    <div class="flex flex-col gap-4 py-2">
      <div class="flex flex-col gap-2">
        <Label for="bulk-approve-duration">Protection Duration</Label>
        <Select.Root type="single" bind:value={bulkDuration}>
          <Select.Trigger
            class="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            {(() => {
              if (bulkDuration === "user_requested")
                return "Use each request's selected duration";
              if (bulkDuration === "forever") return "Forever";
              if (bulkDuration === "custom") return "Custom days";
              if (!isNaN(Number(bulkDuration))) return `${bulkDuration} days`;
              return bulkDuration;
            })()}
          </Select.Trigger>
          <Select.Content class="bg-card border-ring">
            <Select.Item
              value="user_requested"
              label="Use each request's selected duration"
              class="text-foreground"
              >Use each request's selected duration</Select.Item
            >
            <Select.Item value="30" label="30 days" class="text-foreground"
              >30 days</Select.Item
            >
            <Select.Item value="90" label="90 days" class="text-foreground"
              >90 days</Select.Item
            >
            <Select.Item value="180" label="180 days" class="text-foreground"
              >180 days</Select.Item
            >
            <Select.Item value="365" label="1 year" class="text-foreground"
              >1 year</Select.Item
            >
            <Select.Item
              value="custom"
              label="Custom days"
              class="text-foreground">Custom days</Select.Item
            >
            <Select.Item value="forever" label="Forever" class="text-foreground"
              >Forever</Select.Item
            >
          </Select.Content>
        </Select.Root>
        {#if bulkDuration === "custom"}
          <input
            type="number"
            min="1"
            step="1"
            bind:value={bulkCustomDays}
            placeholder="Enter number of days"
            class="h-9 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm
              placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1
              focus-visible:ring-ring"
          />
        {/if}
      </div>
      <div class="flex flex-col gap-2">
        <Label for="bulk-approve-notes">Admin Notes (optional)</Label>
        <textarea
          id="bulk-approve-notes"
          bind:value={bulkNotes}
          rows={3}
          class="flex w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm
            placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1
            focus-visible:ring-ring resize-none"
          placeholder="Applied to all selected requests"
        ></textarea>
      </div>
    </div>
    <Dialog.Footer>
      <Button variant="outline" onclick={() => (bulkApproveDialogOpen = false)}>
        Cancel
      </Button>
      <Button
        onclick={submitBulkApprove}
        disabled={bulkSubmitting || selectedPendingIds.length === 0}
      >
        {#if bulkSubmitting}
          <Spinner size="sm" class="mr-2" />
        {/if}
        Approve Selected
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

<!-- bulk deny dialog -->
<Dialog.Root bind:open={bulkDenyDialogOpen}>
  <Dialog.Content class="sm:max-w-md text-foreground">
    <Dialog.Header>
      <Dialog.Title>Deny Selected</Dialog.Title>
      <Dialog.Description>
        Deny {selectedPendingIds.length} pending request{selectedPendingIds.length ===
        1
          ? ""
          : "s"}.
      </Dialog.Description>
    </Dialog.Header>
    <div class="flex flex-col gap-2 py-2">
      <Label for="bulk-deny-notes">Reason / Admin Notes (optional)</Label>
      <textarea
        id="bulk-deny-notes"
        bind:value={bulkNotes}
        rows={3}
        class="flex w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring resize-none"
        placeholder="Applied to all selected requests"
      ></textarea>
    </div>
    <Dialog.Footer>
      <Button variant="outline" onclick={() => (bulkDenyDialogOpen = false)}>
        Cancel
      </Button>
      <Button
        variant="destructive"
        onclick={submitBulkDeny}
        disabled={bulkSubmitting || selectedPendingIds.length === 0}
      >
        {#if bulkSubmitting}
          <Spinner size="sm" class="mr-2" />
        {/if}
        Deny Selected
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

<div class="p-2.5 md:p-8">
  <div class="max-w-7xl mx-auto">
    <!-- header -->
    <div class="flex items-center justify-between mb-6">
      <div>
        <h1 class="text-3xl font-bold text-foreground mb-1">
          Exception Requests
        </h1>
        <p class="text-muted-foreground text-sm">
          {#if canManageRequests}
            Review and manage requests to protect media from cleanup.
          {:else}
            Your requests to protect media from cleanup.
          {/if}
        </p>
      </div>
    </div>

    <!-- filter controls -->
    <div class="mb-6 grid gap-2 sm:grid-cols-3">
      <Input
        type="text"
        bind:value={searchQuery}
        placeholder="Search title, reason, user..."
        class="input-hover-el"
      />

      <!-- status filter -->
      <Select.Root type="single" bind:value={statusFilter}>
        <Select.Trigger
          class="h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          {(() => {
            if (statusFilter === "all") return "All statuses";
            if (statusFilter === ExceptionRequestStatus.Pending)
              return "Pending";
            if (statusFilter === ExceptionRequestStatus.Approved)
              return "Approved";
            if (statusFilter === ExceptionRequestStatus.Denied) return "Denied";
            return String(statusFilter);
          })()}
        </Select.Trigger>
        <Select.Content class="bg-card border-ring">
          <Select.Item value="all" label="All statuses" class="text-foreground"
            >All statuses</Select.Item
          >
          <Select.Item
            value={ExceptionRequestStatus.Pending}
            label="Pending"
            class="text-foreground">Pending</Select.Item
          >
          <Select.Item
            value={ExceptionRequestStatus.Approved}
            label="Approved"
            class="text-foreground">Approved</Select.Item
          >
          <Select.Item
            value={ExceptionRequestStatus.Denied}
            label="Denied"
            class="text-foreground">Denied</Select.Item
          >
        </Select.Content>
      </Select.Root>

      <!-- sort order -->
      <Select.Root type="single" bind:value={sortOrder}>
        <Select.Trigger
          class="h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          {sortOrder === "desc" ? "Newest first" : "Oldest first"}
        </Select.Trigger>
        <Select.Content class="bg-card border-ring">
          <Select.Item value="desc" label="Newest first" class="text-foreground"
            >Newest first</Select.Item
          >
          <Select.Item value="asc" label="Oldest first" class="text-foreground"
            >Oldest first</Select.Item
          >
        </Select.Content>
      </Select.Root>
    </div>

    <ErrorBox error={error ?? undefined} />

    {#if loading}
      <div class="flex flex-col items-center gap-4 py-16 text-muted-foreground">
        <Spinner size="lg" class="text-primary" />
        <p>Loading requests…</p>
      </div>
    {:else if filteredRequests.length === 0}
      <div class="flex flex-col items-center gap-2 py-16 text-muted-foreground">
        <p class="text-lg font-medium">No requests found</p>
        <p class="text-sm">
          {statusFilter === "all"
            ? "No exception requests have been submitted yet."
            : `No ${statusFilter} requests.`}
        </p>
      </div>
    {:else if canManageRequests}
      <div
        class="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-muted/30 p-3"
      >
        <Button
          size="sm"
          onclick={toggleSelectAllPending}
          disabled={pendingInView.length === 0}
          class="cursor-pointer"
        >
          {allPendingSelected
            ? "Clear Pending Selection"
            : "Select All Pending"}
        </Button>
        <span class="text-sm text-muted-foreground">
          {selectedPendingIds.length} selected
        </span>
        <div class="ml-auto flex gap-2">
          <Button
            size="sm"
            variant="secondary"
            onclick={openBulkApprove}
            disabled={selectedPendingIds.length === 0}
            class="gap-1.5 text-green-600 border-green-600 hover:bg-green-600 hover:text-white 
              cursor-pointer"
          >
            <CheckCircle class="size-4" /> Approve Selected
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onclick={openBulkDeny}
            disabled={selectedPendingIds.length === 0}
            class="gap-1.5 text-destructive border-destructive hover:bg-destructive hover:text-white 
              cursor-pointer"
          >
            <XCircle class="size-4" /> Deny Selected
          </Button>
        </div>
      </div>

      <div class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div class="rounded-lg border border-border overflow-hidden">
          <div class="max-h-[70vh] overflow-y-auto">
            {#each filteredRequests as req (req.id)}
              <button
                class="w-full border-b p-4 text-left transition-colors hover:bg-muted/55 {selectedRequestId ===
                req.id
                  ? 'bg-muted/55 border-b-focus-ring'
                  : ''}"
                onclick={() => (selectedRequestId = req.id)}
              >
                <div class="flex items-start gap-3">
                  {#if req.status === ExceptionRequestStatus.Pending}
                    <input
                      type="checkbox"
                      class="mt-1"
                      checked={selectedIds.includes(req.id)}
                      onclick={(event) => event.stopPropagation()}
                      onchange={() => toggleSelection(req.id)}
                    />
                  {/if}
                  <PosterThumb
                    mediaType={req.media_type}
                    posterUrl={req.poster_url}
                    width={TMDB_POSTER_WIDTH}
                  />
                  <div class="flex-1 min-w-0">
                    <div class="min-w-0">
                      <h3
                        class="font-medium text-foreground line-clamp-3 sm:line-clamp-2"
                      >
                        {req.media_title}
                      </h3>
                      <span class="text-sm text-muted-foreground"
                        >{req.media_year}</span
                      >
                    </div>

                    <p class="mt-1 text-xs text-muted-foreground line-clamp-1">
                      {req.reason}
                    </p>

                    <div
                      class="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
                    >
                      <span>@{req.requested_by_username}</span>
                      <span>{formatDate(req.created_at)}</span>
                      <div
                        class="ml-auto flex items-center gap-1.5 text-foreground"
                      >
                        <MediaTypeBadge mediaType={req.media_type} />
                        <RequestStatusBadge status={req.status} />
                      </div>
                    </div>
                  </div>
                </div>
              </button>
            {/each}
          </div>
        </div>

        <div class="rounded-lg border border-border p-4 lg:p-6">
          {#if selectedRequest}
            <div class="space-y-4">
              <div class="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <h2 class="text-xl font-semibold text-foreground">
                    {selectedRequest.media_title}
                  </h2>
                  <p class="text-sm text-muted-foreground mt-1">
                    Requested {formatDate(selectedRequest.created_at)}
                    by @{selectedRequest.requested_by_username}
                  </p>
                </div>

                <RequestStatusBadge status={selectedRequest.status} />
              </div>

              <div class="space-y-1">
                <p
                  class="text-xs font-medium uppercase tracking-wide text-muted-foreground"
                >
                  Protection
                </p>
                <p class="text-sm text-foreground">
                  {formatEffectiveProtectionLabel(selectedRequest)}
                </p>
                {#if hasProtectionOverride(selectedRequest)}
                  <p class="text-xs text-muted-foreground">
                    Requested: {formatProtectionLabel(selectedRequest)}
                  </p>
                {/if}
              </div>

              <div class="space-y-1">
                <p
                  class="text-xs font-medium uppercase tracking-wide text-muted-foreground"
                >
                  Request Reason
                </p>
                <p
                  class="text-sm text-foreground whitespace-pre-wrap wrap-break-word"
                >
                  {selectedRequest.reason}
                </p>
              </div>

              {#if selectedRequest.admin_notes}
                <div class="space-y-1">
                  <p
                    class="text-xs font-medium uppercase tracking-wide text-muted-foreground"
                  >
                    Admin Notes
                  </p>
                  <p
                    class="text-sm text-foreground whitespace-pre-wrap wrap-break-word"
                  >
                    {selectedRequest.admin_notes}
                  </p>
                </div>
              {/if}

              <div class="pt-2 border-t border-border flex flex-wrap gap-2">
                {#if selectedRequest.status === ExceptionRequestStatus.Pending}
                  <Button
                    size="sm"
                    variant="outline"
                    onclick={() => openApprove(selectedRequest)}
                    class="gap-1.5 text-green-600 border-green-600 hover:bg-green-600 hover:text-white"
                  >
                    <CheckCircle class="w-3.5 h-3.5" />
                    Approve
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onclick={() => openDeny(selectedRequest)}
                    class="gap-1.5 text-destructive border-destructive hover:bg-destructive hover:text-white"
                  >
                    <XCircle class="w-3.5 h-3.5" />
                    Deny
                  </Button>
                {/if}
              </div>
            </div>
          {:else}
            <p class="text-sm text-muted-foreground">
              Select a request to view details.
            </p>
          {/if}
        </div>
      </div>
    {:else}
      <div class="space-y-3">
        {#each filteredRequests as req (req.id)}
          <div class="rounded-lg border border-border p-4">
            <div class="flex items-start gap-3">
              <PosterThumb
                mediaType={req.media_type}
                posterUrl={req.poster_url}
                width={TMDB_POSTER_WIDTH}
              />
              <div class="min-w-0 flex-1">
                <h3
                  class="font-medium text-foreground line-clamp-3 sm:line-clamp-2"
                >
                  {req.media_title}
                </h3>
                <span class="text-sm text-muted-foreground"
                  >{req.media_year}</span
                >
                <p class="mt-1 text-xs text-muted-foreground">
                  Requested {formatDate(req.created_at)} · {formatEffectiveProtectionLabel(
                    req,
                  )}
                </p>
                {#if hasProtectionOverride(req)}
                  <p class="text-xs text-muted-foreground">
                    Requested: {formatProtectionLabel(req)}
                  </p>
                {/if}
                <div class="mt-2 flex flex-wrap items-center gap-1.5">
                  <MediaTypeBadge mediaType={req.media_type} />
                  <RequestStatusBadge status={req.status} />
                </div>
              </div>
            </div>

            <p
              class="mt-3 text-sm text-foreground whitespace-pre-wrap wrap-break-word"
            >
              {req.reason}
            </p>

            {#if req.admin_notes}
              <div class="mt-3 rounded-md bg-muted/40 p-3">
                <p class="text-xs font-medium text-muted-foreground mb-1">
                  Admin Notes
                </p>
                <p
                  class="text-sm text-foreground whitespace-pre-wrap wrap-break-word"
                >
                  {req.admin_notes}
                </p>
              </div>
            {/if}

            {#if req.status === ExceptionRequestStatus.Pending && req.requested_by_user_id === $auth.user?.id}
              <div class="mt-3 pt-3 border-t border-border">
                <Button
                  size="sm"
                  variant="outline"
                  onclick={() => openCancel(req)}
                  class="gap-1.5 text-destructive border-destructive hover:bg-destructive hover:text-white"
                >
                  <Trash2 class="w-3.5 h-3.5" />
                  Cancel Request
                </Button>
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>
