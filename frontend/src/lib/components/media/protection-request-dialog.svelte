<script lang="ts">
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { post_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import { auth } from "$lib/stores/auth";
  import type { ProtectionRequest, MediaType } from "$lib/types/shared";
  import { Permission } from "$lib/types/shared";
  import * as Select from "$lib/components/ui/select/index.js";
  import { Textarea } from "$lib/components/ui/textarea/index.js";
  import { Input } from "$lib/components/ui/input/index.js";

  const TMDB_POSTER_WIDTH = 342;
  const inputPlaceHolderText: string =
    "Explain why this should be kept (e.g., 'Planning to watch " +
    "soon', 'Personal favorite', etc.)";

  interface MediaLike {
    id: number;
    title: string;
    year: number | null;
    poster_url: string | null;
    status: { is_candidate: boolean };
  }

  interface Props {
    open: boolean;
    media: MediaLike | null;
    mediaType: MediaType;
    onClose?: () => void;
    onSuccess?: (request: ProtectionRequest) => void;
  }

  let {
    open = $bindable(),
    media,
    mediaType,
    onClose,
    onSuccess,
  }: Props = $props();

  const isAdmin = $derived(
    $auth.user?.role === "admin" ||
      ($auth.user?.permissions ?? []).includes(Permission.AutoApprove),
  );

  let reason = $state("");
  let submitting = $state(false);
  let duration = $state("30");
  let customDays = $state("30");

  // reset form defaults each time the dialog opens
  $effect(() => {
    if (open) {
      reason = isAdmin ? "Admin decision" : "";
      duration = isAdmin ? "permanent" : "30";
      customDays = "30";
    }
  });

  const durationOptions = [
    { value: "30", label: "30 days" },
    { value: "90", label: "90 days" },
    { value: "180", label: "180 days" },
    { value: "365", label: "1 year" },
    { value: "custom", label: "Custom days" },
    { value: "permanent", label: "Permanent" },
  ];

  const handleSubmit = async () => {
    if (!media) return;
    if (!isAdmin && !reason.trim()) {
      toast.error("Please provide a reason");
      return;
    }

    try {
      submitting = true;

      let durationDays: number | null;
      if (duration === "permanent") {
        durationDays = null;
      } else if (duration === "custom") {
        const parsed = Number(customDays);
        if (!Number.isInteger(parsed) || parsed <= 0) {
          toast.error(
            "Custom duration must be a positive whole number of days",
          );
          submitting = false;
          return;
        }
        durationDays = parsed;
      } else {
        durationDays = Number(duration);
      }

      const createdRequest = await post_api<ProtectionRequest>(
        "/api/protection-requests",
        {
          media_type: mediaType,
          media_id: media.id,
          reason: reason.trim() || null,
          duration_days: durationDays,
        },
      );

      toast.success(
        isAdmin
          ? `"${media.title}" protected from deletion`
          : "Protection request submitted successfully",
      );
      if (onSuccess) onSuccess(createdRequest);
      handleClose(false);
    } catch (err: any) {
      toast.error(`Failed to submit request: ${err.message}`);
    } finally {
      submitting = false;
    }
  };

  const handleClose = (fireCallback: boolean = true) => {
    reason = "";
    duration = isAdmin ? "permanent" : "30";
    customDays = "30";
    open = false;
    if (fireCallback && onClose) {
      onClose();
    }
  };
</script>

<Dialog.Root bind:open>
  <Dialog.Content
    showCloseButton={false}
    class="media-dialog sm:max-w-175 max-h-[90vh] overflow-y-auto border-ring border-2"
  >
    <Dialog.Header>
      <Dialog.Title class="text-foreground">
        {isAdmin ? "Protect from Deletion" : "Request Protection"}
      </Dialog.Title>
      <Dialog.Description class="text-muted-foreground">
        {isAdmin
          ? `Protect this ${mediaType} from being deleted`
          : `Request that this ${mediaType} be protected from deletion`}
      </Dialog.Description>
    </Dialog.Header>

    {#if media}
      <div class="space-y-4 py-4">
        <!-- media info -->
        <div class="flex gap-4">
          {#if media.poster_url}
            <img
              src="http://image.tmdb.org/t/p/w{TMDB_POSTER_WIDTH}/{media.poster_url}"
              alt={media.title}
              class="w-25 h-38 object-cover rounded"
            />
          {/if}
          <div class="flex-1">
            <h3 class="font-semibold text-foreground">{media.title}</h3>
            <p class="text-sm text-muted-foreground">{media.year}</p>
            {#if media.status.is_candidate}
              <p class="text-xs text-yellow-500 mt-2">
                This {mediaType} is currently marked for deletion
              </p>
            {/if}
          </div>
        </div>

        <!-- reason input -->
        {#if !isAdmin}
          <div class="space-y-2">
            <Label for="reason" class="text-foreground">
              <span class="text-red-500">*</span>
            </Label>
            <Textarea
              id="reason"
              bind:value={reason}
              placeholder={inputPlaceHolderText}
              class="w-full min-h-30 px-3 py-2 bg-card text-card-foreground 
                placeholder:text-muted-foreground focus:ring-1 focus:ring-focus-ring resize-none"
              disabled={submitting}
            ></Textarea>
            <p class="text-xs text-muted-foreground">
              Your request will be reviewed by an administrator
            </p>
          </div>
        {/if}

        <!-- duration -->
        <div class="space-y-2">
          <Label class="text-foreground">
            {isAdmin ? "Exclusion duration" : "Protection duration"}
          </Label>
          <Select.Root type="single" bind:value={duration}>
            <Select.Trigger class="w-full">
              {durationOptions.find((opt) => opt.value === duration)?.label}
            </Select.Trigger>
            <Select.Content>
              {#each durationOptions as option}
                <Select.Item
                  value={option.value}
                  label={option.label}
                  class="text-foreground"
                >
                  {option.label}
                </Select.Item>
              {/each}
            </Select.Content>
          </Select.Root>
          {#if duration === "custom"}
            <Input
              type="number"
              min={1}
              step={1}
              bind:value={customDays}
              placeholder="Enter number of days"
              class="input-hover-el"
              disabled={submitting}
            />
          {/if}
          {#if !isAdmin}
            <p class="text-xs text-muted-foreground">
              Admins can override this duration when approving
            </p>
          {/if}
        </div>
      </div>

      <Dialog.Footer>
        <Button
          variant="secondary"
          class="cursor-pointer"
          onclick={() => handleClose()}
          disabled={submitting}
        >
          Cancel
        </Button>
        <Button
          class="cursor-pointer"
          onclick={handleSubmit}
          disabled={submitting || (!isAdmin && !reason.trim())}
        >
          {#if submitting}
            {isAdmin ? "Protecting..." : "Submitting..."}
          {:else}
            {isAdmin ? "Protect from Deletion" : "Request Protection"}
          {/if}
        </Button>
      </Dialog.Footer>
    {/if}
  </Dialog.Content>
</Dialog.Root>
