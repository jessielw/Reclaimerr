<script lang="ts">
  import { Input } from "$lib/components/ui/input/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import { ProtectionRequestStatus } from "$lib/types/shared";
  import Search from "@lucide/svelte/icons/search";

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

<div class="mb-4 flex flex-col sm:flex-row gap-2">
  <div class="relative flex-1">
    <Search
      class="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted-foreground"
    />
    <Input
      type="text"
      bind:value={searchQuery}
      placeholder={searchPlaceholder}
      class="pl-10 bg-card text-card-foreground placeholder:text-muted-foreground"
    />
  </div>

  <div class="flex flex-1 gap-2">
    <Select.Root type="single" bind:value={statusFilter}>
      <Select.Trigger
        class="flex-1 bg-card text-card-foreground cursor-pointer"
      >
        {statusLabel}
      </Select.Trigger>
      <Select.Content class="bg-card">
        <Select.Item
          value="all"
          label="All statuses"
          class="text-card-foreground cursor-pointer">All statuses</Select.Item
        >
        <Select.Item
          value={ProtectionRequestStatus.Pending}
          label="Pending"
          class="text-card-foreground cursor-pointer">Pending</Select.Item
        >
        <Select.Item
          value={ProtectionRequestStatus.Approved}
          label="Approved"
          class="text-card-foreground cursor-pointer">Approved</Select.Item
        >
        <Select.Item
          value={ProtectionRequestStatus.Denied}
          label="Denied"
          class="text-card-foreground cursor-pointer">Denied</Select.Item
        >
      </Select.Content>
    </Select.Root>

    <Select.Root type="single" bind:value={sortOrder}>
      <Select.Trigger
        class="flex-1 bg-card text-card-foreground cursor-pointer"
      >
        {sortOrder === "desc" ? "Newest first" : "Oldest first"}
      </Select.Trigger>
      <Select.Content class="bg-card">
        <Select.Item
          value="desc"
          label="Newest first"
          class="text-card-foreground cursor-pointer">Newest first</Select.Item
        >
        <Select.Item
          value="asc"
          label="Oldest first"
          class="text-card-foreground cursor-pointer">Oldest first</Select.Item
        >
      </Select.Content>
    </Select.Root>
  </div>
</div>
