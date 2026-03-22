<script lang="ts">
  import { Badge } from "$lib/components/ui/badge/index.js";
  import { MediaType } from "$lib/types/shared";
  import { toTitleCase } from "$lib/utils/strings";

  let { mediaType }: { mediaType: MediaType } = $props();
</script>

<Badge class={mediaType === MediaType.Movie ? "bg-movie" : "bg-series"}>
  {toTitleCase(mediaType)}
</Badge>
