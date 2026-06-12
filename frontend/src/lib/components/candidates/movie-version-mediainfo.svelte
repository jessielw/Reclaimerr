<script lang="ts">
  import type { ReclaimCandidateEntry } from "$lib/types/shared";
  import { formatFileSize } from "$lib/utils/formatters";
  import Badge from "$lib/components/ui/badge/badge.svelte";
  import { toTitleCase } from "$lib/utils/strings";
  import {
    extractPathNoFile,
    fileNameFromPath,
  } from "$lib/utils/candidate-rules";
  import { candidateMediaMetaFields } from "$lib/components/candidates/view-utils";

  type DetailField = { label: string; value: string };
  type DetailSection = { title: string; fields: DetailField[] };

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

  const parseRuleParts = (
    tokens: { rule_name: string }[] | null | undefined,
  ): string[] => {
    if (!tokens) return [];
    const parts: string[] = [];
    tokens.forEach((token) => {
      const name = token.rule_name?.trim();
      if (name) {
        parts.push(name);
      }
    });
    return parts;
  };

  const fileFields = (item: ReclaimCandidateEntry): DetailField[] => [
    {
      label: "Name",
      value: fileNameFromPath(item.version_path, item.version_file_name),
    },
    { label: "Size", value: formatFileSize(item.version_size) },
    {
      label: "Path",
      value: textValue(extractPathNoFile(item.version_path || "")),
    },
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
    {
      label: "Service",
      value: textValue(toTitleCase(item.version_service || "")),
    },
    ...candidateMediaMetaFields(item, formatDate),
  ];

  const visibleFields = (fields: DetailField[]): DetailField[] =>
    fields.filter((field) => field.value !== unknownValue);

  const sections = $derived<DetailSection[]>([
    { title: "File", fields: visibleFields(fileFields(entry)) },
    { title: "Video", fields: visibleFields(videoFields(entry)) },
    { title: "Audio", fields: visibleFields(audioFields(entry)) },
    { title: "Languages", fields: visibleFields(languageFields(entry)) },
    { title: "Source", fields: visibleFields(sourceFields(entry)) },
  ]);

  const visibleSections = $derived(
    sections.filter((section) => section.fields.length > 0),
  );

  const ruleNames = $derived(parseRuleParts(entry.reason_parts));
</script>

<div class="space-y-3">
  {#each visibleSections as section}
    <section class="rounded border border-border/70 bg-muted/20 p-3">
      <h4 class="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">
        {section.title}
      </h4>
      <div class="space-y-1.5">
        {#each section.fields as field}
          <div class="min-w-0">
            <div class="text-xs text-muted-foreground">{field.label}</div>
            <div class="text-sm leading-6 text-foreground break-all">
              {field.value}
            </div>
          </div>
        {/each}
      </div>
    </section>
  {/each}

  <!-- rules -->
  {#if ruleNames.length > 0}
    <section class="rounded border border-border/70 bg-muted/20 p-3">
      <h4 class="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">
        Rules
      </h4>
      <div class="flex flex-wrap gap-1.5">
        {#each ruleNames as rule}
          <Badge
            class="border-primary break-all whitespace-normal"
            variant="secondary">{rule}</Badge
          >
        {/each}
      </div>
    </section>
  {/if}
</div>
