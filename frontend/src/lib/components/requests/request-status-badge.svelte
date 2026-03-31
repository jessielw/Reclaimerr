<script lang="ts">
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { ProtectionRequestStatus } from "$lib/types/shared";

  let { status }: { status: ProtectionRequestStatus } = $props();
</script>

{#if status === ProtectionRequestStatus.Pending}
  <Badge variant="outline" class="border-yellow-500 text-yellow-500">
    Pending
  </Badge>
{:else if status === ProtectionRequestStatus.Approved}
  <Badge variant="outline" class="border-green-500 text-green-500">
    Approved
  </Badge>
{:else}
  <Badge variant="outline" class="border-destructive text-destructive">
    Denied
  </Badge>
{/if}
