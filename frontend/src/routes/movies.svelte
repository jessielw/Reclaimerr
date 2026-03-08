<script lang="ts">
  import MediaBrowser from "$lib/components/media/MediaBrowser.svelte";
  import { MediaType } from "$lib/types/shared";
</script>

<MediaBrowser mediaType={MediaType.Movie} />
