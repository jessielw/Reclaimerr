<script lang="ts">
  import type { ReclaimCandidateEntry } from "$lib/types/shared";
  import { formatFileSize } from "$lib/utils/formatters";

  type DetailField = { label: string; value: string };

  interface Props {
    entry: ReclaimCandidateEntry;
    formatDate: (value: string) => string;
  }

  let { entry, formatDate }: Props = $props();

  const unknownValue = "Unknown";

  const textValue = (value: string | number | null | undefined): string => {
    if (value == null) return unknownValue;
    const str = String(value).trim();
    return str.length > 0 ? str : unknownValue;
  };

  const boolValue = (value: boolean | null | undefined): string => {
    if (value == null) return unknownValue;
    return value ? "Yes" : "No";
  };

  const resolutionValue = (item: ReclaimCandidateEntry): string => {
    if (item.version_video_width && item.version_video_height) {
      return `${item.version_video_width}x${item.version_video_height}`;
    }
    return unknownValue;
  };

  const fileNameFromPath = (
    path: string | null,
    fallbackFileName: string | null = null,
  ): string => {
    if (fallbackFileName && fallbackFileName.trim()) {
      return fallbackFileName.trim();
    }
    if (!path) return unknownValue;
    const parts = path.split(/[/\\]/);
    const extractedFileName = parts[parts.length - 1];
    return extractedFileName?.trim() ? extractedFileName : unknownValue;
  };

  const parseRuleTokens = (tokens: string[] | null | undefined): string[] =>
    (tokens ?? []).map((token) => token.trim()).filter(Boolean);

  const fileFields = (item: ReclaimCandidateEntry): DetailField[] => [
    {
      label: "Name",
      value: fileNameFromPath(item.version_path, item.version_file_name),
    },
    { label: "Size", value: formatFileSize(item.version_size) },
    { label: "Path", value: textValue(item.version_path) },
  ];

  const videoFields = (item: ReclaimCandidateEntry): DetailField[] => [
    { label: "Resolution", value: resolutionValue(item) },
    {
      label: "Codec",
      value: textValue(item.version_video_codec_family?.toUpperCase()),
    },
    { label: "HDR", value: boolValue(item.version_video_hdr) },
    {
      label: "Dolby Vision",
      value: boolValue(item.version_video_dolby_vision),
    },
  ];

  const audioFields = (item: ReclaimCandidateEntry): DetailField[] => [
    {
      label: "Codec",
      value: textValue(item.version_audio_codec_family?.toUpperCase()),
    },
    { label: "Channels", value: textValue(item.version_audio_channels) },
  ];

  const languageFields = (item: ReclaimCandidateEntry): DetailField[] => [
    {
      label: "Audio Languages",
      value: item.version_audio_languages?.length
        ? item.version_audio_languages.join(", ")
        : unknownValue,
    },
    {
      label: "Subtitle Languages",
      value: item.version_subtitle_languages?.length
        ? item.version_subtitle_languages.join(", ")
        : unknownValue,
    },
  ];

  const sourceFields = (item: ReclaimCandidateEntry): DetailField[] => [
    { label: "Service", value: textValue(item.version_service) },
    { label: "Library", value: textValue(item.version_library_name) },
    { label: "Flagged", value: formatDate(item.created_at) },
  ];

  const rules = $derived(parseRuleTokens(entry.reason_tokens));
</script>

<div class="space-y-3">
  <section class="rounded border border-border/70 bg-muted/20 p-3">
    <h4 class="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">
      File
    </h4>
    <div class="space-y-1.5">
      {#each fileFields(entry) as field}
        <div class="min-w-0">
          <div class="text-xs text-muted-foreground">{field.label}</div>
          <div class="text-sm leading-6 text-foreground break-all">
            {field.value}
          </div>
        </div>
      {/each}
    </div>
  </section>

  <section class="rounded border border-border/70 bg-muted/20 p-3">
    <h4 class="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">
      Video
    </h4>
    <div class="space-y-1.5">
      {#each videoFields(entry) as field}
        <div class="min-w-0">
          <div class="text-xs text-muted-foreground">{field.label}</div>
          <div class="text-sm leading-6 text-foreground break-all">
            {field.value}
          </div>
        </div>
      {/each}
    </div>
  </section>

  <section class="rounded border border-border/70 bg-muted/20 p-3">
    <h4 class="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">
      Audio
    </h4>
    <div class="space-y-1.5">
      {#each audioFields(entry) as field}
        <div class="min-w-0">
          <div class="text-xs text-muted-foreground">{field.label}</div>
          <div class="text-sm leading-6 text-foreground break-all">
            {field.value}
          </div>
        </div>
      {/each}
    </div>
  </section>

  <section class="rounded border border-border/70 bg-muted/20 p-3">
    <h4 class="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">
      Languages
    </h4>
    <div class="space-y-1.5">
      {#each languageFields(entry) as field}
        <div class="min-w-0">
          <div class="text-xs text-muted-foreground">{field.label}</div>
          <div class="text-sm leading-6 text-foreground break-all">
            {field.value}
          </div>
        </div>
      {/each}
    </div>
  </section>

  <section class="rounded border border-border/70 bg-muted/20 p-3">
    <h4 class="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">
      Source
    </h4>
    <div class="space-y-1.5">
      {#each sourceFields(entry) as field}
        <div class="min-w-0">
          <div class="text-xs text-muted-foreground">{field.label}</div>
          <div class="text-sm leading-6 text-foreground break-all">
            {field.value}
          </div>
        </div>
      {/each}
    </div>
  </section>

  <section class="rounded border border-border/70 bg-muted/20 p-3">
    <h4 class="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">
      Rules
    </h4>
    <div class="flex flex-wrap gap-1.5">
      {#if rules.length > 0}
        {#each rules as rule}
          <span
            class="text-xs leading-6 px-2.5 rounded-full border border-border bg-card text-foreground"
          >
            {rule}
          </span>
        {/each}
      {:else}
        <span class="text-sm text-muted-foreground">{unknownValue}</span>
      {/if}
    </div>
  </section>
</div>
