<script lang="ts">
  import type { ReclaimCandidateEntry } from "$lib/types/shared";
  import { formatFileSize } from "$lib/utils/formatters";
  import Badge from "$lib/components/ui/badge/badge.svelte";
  import { toTitleCase } from "$lib/utils/strings";
  import { ruleNames } from "$lib/utils/candidate-rules";
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
    if (item.season_max_video_width && item.season_max_video_height) {
      return `${item.season_max_video_width}x${item.season_max_video_height}`;
    }
    return unknownValue;
  };

  const videoFields = (item: ReclaimCandidateEntry): DetailField[] => [
    { label: "Max Resolution", value: resolutionValue(item) },
    {
      label: "Codec Families",
      value: item.season_video_codec_families?.length
        ? item.season_video_codec_families
            .map((v) => v.toUpperCase())
            .join(", ")
        : unknownValue,
    },
    { label: "HDR", value: boolValue(item.season_has_hdr) },
    { label: "Dolby Vision", value: boolValue(item.season_has_dolby_vision) },
  ];

  const audioFields = (item: ReclaimCandidateEntry): DetailField[] => [
    {
      label: "Codec Families",
      value: item.season_audio_codec_families?.length
        ? item.season_audio_codec_families
            .map((v) => v.toUpperCase())
            .join(", ")
        : unknownValue,
    },
  ];

  const languageFields = (item: ReclaimCandidateEntry): DetailField[] => [
    {
      label: "Audio Languages",
      value: item.season_audio_languages?.length
        ? item.season_audio_languages.join(", ")
        : unknownValue,
    },
    {
      label: "Subtitle Languages",
      value: item.season_subtitle_languages?.length
        ? item.season_subtitle_languages.join(", ")
        : unknownValue,
    },
  ];

  const sourceFields = (item: ReclaimCandidateEntry): DetailField[] => [
    {
      label: "Estimated Size",
      value: formatFileSize(item.estimated_space_bytes),
    },
    {
      label: "Services",
      value: item.series_library_refs?.length
        ? item.series_library_refs
            .map((ref) => toTitleCase(ref.service ?? ""))
            .filter(Boolean)
            .join(", ")
        : unknownValue,
    },
    ...candidateMediaMetaFields(item, formatDate),
    { label: "Season", value: textValue(item.season_number) },
  ];

  const visibleFields = (fields: DetailField[]): DetailField[] =>
    fields.filter((field) => field.value !== unknownValue);

  const sections = $derived<DetailSection[]>([
    { title: "Video", fields: visibleFields(videoFields(entry)) },
    { title: "Audio", fields: visibleFields(audioFields(entry)) },
    { title: "Languages", fields: visibleFields(languageFields(entry)) },
    { title: "Source", fields: visibleFields(sourceFields(entry)) },
  ]);

  const visibleSections = $derived(
    sections.filter((section) => section.fields.length > 0),
  );

  const rules = $derived(ruleNames(entry));
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

  <section class="rounded border border-border/70 bg-muted/20 p-3">
    <h4 class="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">
      Rules
    </h4>
    <div class="flex flex-wrap gap-1.5">
      {#if rules.length > 0}
        {#each rules as rule}
          <Badge variant="secondary">{rule}</Badge>
        {/each}
      {:else}
        <span class="text-sm text-muted-foreground">{unknownValue}</span>
      {/if}
    </div>
  </section>
</div>
