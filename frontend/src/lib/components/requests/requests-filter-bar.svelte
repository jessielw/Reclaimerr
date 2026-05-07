<script lang="ts">
  import { Input } from "$lib/components/ui/input/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import { ProtectionRequestStatus } from "$lib/types/shared";

  type StatusFilter = "all" | ProtectionRequestStatus;
  type SortOrder = "desc" | "asc";

  interface Props {
    searchQuery?: string;
    statusFilter?: StatusFilter;
    sortOrder?: SortOrder;
    searchPlaceholder?: string;
  }

  let {
    searchQuery = $bindable(""),
    statusFilter = $bindable<StatusFilter>(ProtectionRequestStatus.Pending),
    sortOrder = $bindable<SortOrder>("desc"),
    searchPlaceholder = "Search title, reason, user...",
  }: Props = $props();

  const statusLabel = $derived.by(() => {
    if (statusFilter === "all") return "All statuses";
    if (statusFilter === ProtectionRequestStatus.Pending) return "Pending";
    if (statusFilter === ProtectionRequestStatus.Approved) return "Approved";
    if (statusFilter === ProtectionRequestStatus.Denied) return "Denied";
    return String(statusFilter);
  });
</script>

<div class="flex flex-col gap-3 rounded-xl border bg-card p-4">
  <div class="flex flex-col gap-3 lg:flex-row text-foreground">
    <Input
      type="text"
      bind:value={searchQuery}
      placeholder={searchPlaceholder}
      class="input-hover-el lg:flex-1"
    />
    <Select.Root type="single" bind:value={statusFilter}>
      <Select.Trigger class="cursor-pointer">
        {statusLabel}
      </Select.Trigger>
      <Select.Content class="bg-card border-ring">
        <Select.Item value="all" label="All statuses" class="cursor-pointer"
          >All statuses</Select.Item
        >
        <Select.Item
          value={ProtectionRequestStatus.Pending}
          label="Pending"
          class="cursor-pointer">Pending</Select.Item
        >
        <Select.Item
          value={ProtectionRequestStatus.Approved}
          label="Approved"
          class="cursor-pointer">Approved</Select.Item
        >
        <Select.Item
          value={ProtectionRequestStatus.Denied}
          label="Denied"
          class="cursor-pointer">Denied</Select.Item
        >
      </Select.Content>
    </Select.Root>
    <Select.Root type="single" bind:value={sortOrder}>
      <Select.Trigger class="cursor-pointer">
        {sortOrder === "desc" ? "Newest first" : "Oldest first"}
      </Select.Trigger>
      <Select.Content class="bg-card border-ring">
        <Select.Item value="desc" label="Newest first" class="cursor-pointer"
          >Newest first</Select.Item
        >
        <Select.Item value="asc" label="Oldest first" class="cursor-pointer"
          >Oldest first</Select.Item
        >
      </Select.Content>
    </Select.Root>
  </div>
</div>
