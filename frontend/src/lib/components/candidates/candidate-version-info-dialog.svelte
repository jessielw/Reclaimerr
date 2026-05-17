<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import VersionMediaInfoWidget from "$lib/components/candidates/movie-version-mediainfo.svelte";
  import type { ReclaimCandidateEntry } from "$lib/types/shared";
  import X from "@lucide/svelte/icons/x";
  import {
    candidateFileName,
    versionResolutionLabel,
  } from "$lib/components/candidates/view-utils";

  interface Props {
    entry: ReclaimCandidateEntry | null;
    formatDate: (value: string) => string;
    onClose: () => void;
  }

  let { entry, formatDate, onClose }: Props = $props();
</script>

{#if entry}
  <div class="fixed inset-0 z-50">
    <button
      type="button"
      aria-label="Close details"
      class="absolute inset-0 bg-background/70 backdrop-blur-[1px]"
      onclick={onClose}
    ></button>
    <div
      class="relative mx-auto mt-8 w-[min(92vw,820px)] rounded-md border border-border bg-card shadow-xl"
      role="dialog"
      aria-modal="true"
      tabindex="-1"
    >
      <div
        class="flex items-center justify-between border-b border-border px-4 py-3"
      >
        <div class="min-w-0">
          <div class="text-sm font-semibold text-foreground truncate">
            {entry.version_library_name ?? "Version"} - {versionResolutionLabel(
              entry,
            )}
          </div>
          <div class="text-xs text-muted-foreground truncate">
            {candidateFileName(entry.version_path, entry.version_file_name)}
          </div>
        </div>
        <Button
          size="icon"
          class="cursor-pointer rounded-full size-8"
          onclick={onClose}
        >
          <X class="size-4" />
        </Button>
      </div>
      <div class="max-h-[75vh] overflow-y-auto p-4">
        <VersionMediaInfoWidget {entry} {formatDate} />
      </div>
    </div>
  </div>
{/if}
