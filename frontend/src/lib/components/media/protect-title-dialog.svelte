<script lang="ts">
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import { post_api } from "$lib/api";
  import { MediaType, type ProtectedEntry } from "$lib/types/shared";
  import { toast } from "svelte-sonner";

  let {
    open = $bindable(false),
    onSuccess,
  }: {
    open?: boolean;
    onSuccess: () => void;
  } = $props();
  let mediaType = $state<MediaType>(MediaType.Movie);
  let provider = $state("tmdb_id");
  let externalId = $state("");
  let reason = $state("");
  let submitting = $state(false);
  let error = $state("");

  $effect(() => {
    if (open) {
      externalId = "";
      reason = "";
      error = "";
    }
  });
  $effect(() => {
    if (mediaType === MediaType.Movie && provider === "tvdb_id")
      provider = "tmdb_id";
  });

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    const value = externalId.trim();
    if (
      provider === "imdb_id"
        ? !/^tt[0-9]+$/.test(value)
        : !/^[1-9][0-9]*$/.test(value)
    ) {
      error =
        provider === "imdb_id"
          ? "Enter an IMDb ID such as tt0137523."
          : "Enter a positive numeric ID.";
      return;
    }
    submitting = true;
    error = "";
    try {
      const entry = await post_api<ProtectedEntry>("/api/protected", {
        media_type: mediaType,
        [provider]:
          provider === "imdb_id" || provider === "tvdb_id"
            ? value
            : Number(value),
        reason: reason.trim() || null,
        duration_days: null,
      });
      toast.success(`"${entry.media_title}" protected in all libraries`);
      open = false;
      onSuccess();
    } catch (err: any) {
      error = err.message ?? "Failed to protect title.";
    } finally {
      submitting = false;
    }
  }
</script>

<Dialog.Root bind:open>
  <Dialog.Content class="bg-card text-foreground border-border">
    <Dialog.Header>
      <Dialog.Title>Protect entire title</Dialog.Title>
      <Dialog.Description>
        Permanently protect a synced title in every library, including future
        versions, replacement files, and all episodes of a series. Manage its
        duration on the Protected page.
      </Dialog.Description>
    </Dialog.Header>
    <form onsubmit={submit} class="space-y-4">
      <div class="space-y-2">
        <Label for="protect-title-type">Media type</Label>
        <select
          id="protect-title-type"
          bind:value={mediaType}
          class="w-full rounded-md border border-input bg-card p-2"
        >
          <option value={MediaType.Movie}>Movie</option>
          <option value={MediaType.Series}>Series</option>
        </select>
      </div>
      <div class="space-y-2">
        <Label for="protect-title-provider">ID provider</Label>
        <select
          id="protect-title-provider"
          bind:value={provider}
          class="w-full rounded-md border border-input bg-card p-2"
        >
          <option value="tmdb_id">TMDB</option>
          <option value="imdb_id">IMDb</option>
          {#if mediaType === MediaType.Series}<option value="tvdb_id"
              >TVDB</option
            >{/if}
          <option value="anilist_id">AniList</option>
        </select>
      </div>
      <div class="space-y-2">
        <Label for="protect-title-id">Title ID</Label>
        <Input
          id="protect-title-id"
          bind:value={externalId}
          required
          maxlength={20}
          placeholder={provider === "imdb_id" ? "tt0137523" : "550"}
        />
        <p class="text-xs text-muted-foreground">
          The ID must be present in Reclaimerr's synced metadata.
        </p>
      </div>
      <div class="space-y-2">
        <Label for="protect-title-reason">Reason (optional)</Label>
        <Input id="protect-title-reason" bind:value={reason} />
      </div>
      {#if error}<p role="alert" class="text-sm text-destructive">
          {error}
        </p>{/if}
      <Dialog.Footer>
        <Button type="button" variant="outline" onclick={() => (open = false)}
          >Cancel</Button
        >
        <Button type="submit" disabled={submitting || !externalId.trim()}
          >{submitting ? "Protecting…" : "Protect title"}</Button
        >
      </Dialog.Footer>
    </form>
  </Dialog.Content>
</Dialog.Root>
