<script lang="ts">
  import { onMount } from "svelte";
  import { get_api, post_api, delete_api } from "$lib/api";
  import ErrorBox from "$lib/components/error-box.svelte";
  import Spinner from "$lib/components/ui/spinner.svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import RequestsFilterBar from "$lib/components/requests/requests-filter-bar.svelte";
  import MediaTypeBadge from "$lib/components/requests/media-type-badge.svelte";
  import PosterThumb from "$lib/components/requests/poster-thumb.svelte";
  import RequestStatusBadge from "$lib/components/requests/request-status-badge.svelte";
  import { toast } from "svelte-sonner";
  import { auth } from "$lib/stores/auth";
  import {
    type ProtectionRequest,
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
  let requests = $state<ProtectionRequest[]>([]);
  let statusFilter = $state<StatusFilter>(ProtectionRequestStatus.Pending);
  let searchQuery = $state("");
  let sortOrder = $state<SortOrder>("desc");
  let selectedRequestId = $state<number | null>(null);
  let selectedIds = $state<number[]>([]);

  // approve dialog state
  let approveDialogOpen = $state(false);
  let approveTarget = $state<ProtectionRequest | null>(null);
  let approveNotes = $state("");
  let approveDuration = $state("user_requested");
  let approveCustomDays = $state("30");
  let approveSubmitting = $state(false);

  // deny dialog state
  let denyDialogOpen = $state(false);
  let denyTarget = $state<ProtectionRequest | null>(null);
  let denyNotes = $state("");
  let denySubmitting = $state(false);

  // cancel confirmation state
  let cancelTarget = $state<ProtectionRequest | null>(null);
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

  const selectedRequest = $derived(
    filteredRequests.find((r) => r.id === selectedRequestId) ?? null,
  );

  const pendingInView = $derived(
    filteredRequests.filter(
      (r) => r.status === ProtectionRequestStatus.Pending,
    ),
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
        requests = await get_api<ProtectionRequest[]>(
          "/api/protection-requests",
        );
      } else {
        requests = await get_api<ProtectionRequest[]>(
          "/api/protection-requests/my",
        );
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
  const openApprove = (req: ProtectionRequest) => {
    approveTarget = req;
    approveNotes = "";
    approveDuration = "user_requested";
    approveCustomDays = "30";
    approveDialogOpen = true;
  };

  const openDeny = (req: ProtectionRequest) => {
    denyTarget = req;
    denyNotes = "";
    denyDialogOpen = true;
  };

  const openCancel = (req: ProtectionRequest) => {
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
        // leave both null - backend uses the user's original requested_expires_at
      } else if (approveDuration === "permanent") {
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

      await post_api(`/api/protection-requests/${approveTarget.id}/approve`, {
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
      await post_api(`/api/protection-requests/${denyTarget.id}/deny`, {
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
      await delete_api(`/api/protection-requests/${cancelTarget.id}`);
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
      } else if (bulkDuration === "permanent") {
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
          await post_api(`/api/protection-requests/${requestId}/approve`, {
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
          await post_api(`/api/protection-requests/${requestId}/deny`, {
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

  const formatProtectionLabel = (req: ProtectionRequest): string => {
    if (!req.requested_expires_at) return "Permanent";
    return `Until ${formatDate(req.requested_expires_at)}`;
  };

  const formatEffectiveProtectionLabel = (req: ProtectionRequest): string => {
    if (req.status !== ProtectionRequestStatus.Approved) {
      return formatProtectionLabel(req);
    }

    if (req.effective_permanent === true) return "Permanent";
    if (req.effective_expires_at)
      return `Until ${formatDate(req.effective_expires_at)}`;

    return formatProtectionLabel(req);
  };

  const hasProtectionOverride = (req: ProtectionRequest): boolean => {
    return (
      req.status === ProtectionRequestStatus.Approved &&
      formatEffectiveProtectionLabel(req) !== formatProtectionLabel(req)
    );
  };

  const formatTarget = (req: ProtectionRequest): string => {
    if (req.episode_number != null) {
      const label = `S${String(req.season_number ?? 0).padStart(2, "0")}E${String(req.episode_number).padStart(2, "0")}`;
      return req.episode_name ? `${label} "${req.episode_name}"` : label;
    }
    if (req.season_number != null) return `Season ${req.season_number}`;
    if (req.movie_version_id != null) return "Specific version";
    return req.media_type === "movie" ? "Whole movie" : "Whole series";
  };

  const requestScope = (req: ProtectionRequest): string => {
    if (req.target_scope) return req.target_scope;
    if (req.episode_number != null) return "episode";
    if (req.season_number != null) return "season";
    if (req.movie_version_id != null) return "movie_version";
    return req.media_type === "movie" ? "movie" : "series";
  };

  const targetDetailsHeading = (req: ProtectionRequest): string => {
    if (requestScope(req) === "episode") return "Episode Details";
    if (requestScope(req) === "season") return "Season Details";
    return "Version Details";
  };

  const hasTargetDetails = (req: ProtectionRequest): boolean =>
    ["movie_version", "season", "episode"].includes(requestScope(req));

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
                return `Use requested (${approveTarget ? formatProtectionLabel(approveTarget) : "-"})`;
              if (approveDuration === "permanent") return "Permanent";
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
                : "Use requested (-)"}
              class="text-foreground"
              >{approveTarget
                ? `Use requested (${formatProtectionLabel(approveTarget)})`
                : "Use requested (-)"}</Select.Item
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
            <Select.Item
              value="permanent"
              label="Permanent"
              class="text-foreground">Permanent</Select.Item
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
          Denying protection request for <strong
            >{denyTarget.media_title}</strong
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
              if (bulkDuration === "permanent") return "Permanent";
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
            <Select.Item
              value="permanent"
              label="Permanent"
              class="text-foreground">Permanent</Select.Item
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

<div class="px-2.5 md:px-8 pb-2.5 md:pb-8 pt-4 md:pt-4">
  <div class="max-w-7xl mx-auto space-y-6">
    <!-- filter controls -->
    <RequestsFilterBar bind:searchQuery bind:statusFilter bind:sortOrder />

    <ErrorBox error={error ?? undefined} />

    {#if loading}
      <div class="flex items-center justify-center py-20">
        <Spinner class="size-10 text-primary" />
      </div>
    {:else if filteredRequests.length === 0}
      <div class="rounded-xl border bg-card p-10 text-center">
        <p class="text-lg font-medium text-foreground">
          No protection requests found
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
                  {#if req.status === ProtectionRequestStatus.Pending}
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
                    posterSize={"154"}
                    tailWindElSize="w-28"
                    showMediaType={true}
                  />
                  <div class="flex-1 min-w-0">
                    <div class="min-w-0">
                      <h3
                        class="font-medium text-foreground line-clamp-3 sm:line-clamp-2"
                      >
                        {req.media_title}
                      </h3>
                      <span class="text-sm text-muted-foreground"
                        >{req.media_year ?? "Unknown"}</span
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
                  Target
                </p>
                <p class="text-sm text-foreground">
                  {formatTarget(selectedRequest)}
                </p>
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

              {#if hasTargetDetails(selectedRequest)}
                <div class="text-foreground/75">
                  <p class="mb-2 text-sm font-medium text-foreground">
                    {targetDetailsHeading(selectedRequest)}
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
                {#if selectedRequest.status === ProtectionRequestStatus.Pending}
                  <Button
                    size="sm"
                    onclick={() => openApprove(selectedRequest)}
                    class="cursor-pointer"
                  >
                    <CheckCircle class="w-3.5 h-3.5" />
                    Approve
                  </Button>
                  <Button
                    size="sm"
                    variant="destructive"
                    onclick={() => openDeny(selectedRequest)}
                    class="cursor-pointer"
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
                posterSize={"154"}
                tailWindElSize="w-28"
                showMediaType={true}
              />
              <div class="min-w-0 flex-1">
                <h3
                  class="font-medium text-foreground line-clamp-3 sm:line-clamp-2"
                >
                  {req.media_title}
                </h3>
                <span class="text-sm text-muted-foreground"
                  >{req.media_year ?? "Unknown"}</span
                >
                <p class="mt-1 text-xs text-muted-foreground">
                  Requested {formatDate(req.created_at)} · {formatEffectiveProtectionLabel(
                    req,
                  )}
                </p>
                <p class="text-xs text-muted-foreground">
                  Target: {formatTarget(req)}
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
                {#if req.movie_version_id != null || req.season_id != null}
                  <div class="mt-1.5 flex flex-wrap gap-1 text-foreground/75">
                    <!-- resolution -->
                    {#if req.version_resolution ?? req.season_resolution}
                      <span
                        class="rounded-full border bg-muted px-2 py-0 text-xs"
                        >{cleanResolutionString(
                          req.version_resolution ?? req.season_resolution,
                        )}</span
                      >
                    {/if}
                    <!-- video codec -->
                    {#if req.version_video_codec}
                      <span
                        class="rounded-full border bg-muted px-2 py-0 text-xs"
                        >{req.version_video_codec.toUpperCase()}</span
                      >
                    {/if}
                    <!-- season video codecs -->
                    {#if req.season_video_codecs?.length}
                      {#each req.season_video_codecs as codec}
                        <span
                          class="rounded-full border bg-muted px-2 py-0 text-xs"
                          >{codec.toUpperCase()}</span
                        >
                      {/each}
                    {/if}
                    <!-- HDR -->
                    {#if req.version_hdr ?? req.season_hdr}
                      <span
                        class="rounded-full border bg-muted px-2.5 py-0.5 text-xs font-medium"
                        >HDR</span
                      >
                    {/if}
                    <!-- dolby visions -->
                    {#if req.version_dolby_vision ?? req.season_dolby_vision}
                      <span
                        class="rounded-full border bg-muted px-2.5 py-0.5 text-xs font-medium"
                        >Dolby Vision</span
                      >
                    {/if}
                    <!-- file size -->
                    {#if req.version_size ?? req.season_size}
                      <span
                        class="rounded-full border bg-muted px-2 py-0 text-xs"
                        >{formatFileSize(
                          req.version_size ?? req.season_size,
                        )}</span
                      >
                    {/if}
                  </div>
                {/if}
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

            {#if req.status === ProtectionRequestStatus.Pending && req.requested_by_user_id === $auth.user?.id}
              <div class="mt-3 pt-3 border-t border-border">
                <Button
                  size="sm"
                  variant="destructive"
                  onclick={() => openCancel(req)}
                  class="cursor-pointer"
                >
                  <Trash2 class="size-4" />
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
