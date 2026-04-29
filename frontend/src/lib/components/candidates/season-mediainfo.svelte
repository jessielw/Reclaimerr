<script lang="ts">
  import type { ReclaimCandidateEntry } from "$lib/types/shared";

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

  const gbValue = (value: number | null): string =>
    value != null ? `${value.toFixed(2)} GB` : unknownValue;

  const resolutionValue = (item: ReclaimCandidateEntry): string => {
    if (item.season_max_video_width && item.season_max_video_height) {
      return `${item.season_max_video_width}x${item.season_max_video_height}`;
    }
    return unknownValue;
  };

  const parseRuleTokens = (reason: string | null | undefined): string[] => {
    if (!reason) return [];
    const cleaned = reason.replace(/\s+/g, " ").trim();
    if (!cleaned) return [];
    const tokens = cleaned
      .split(/[\n;|]+/)
      .map((token) => token.trim())
      .filter(Boolean);
    return tokens.length > 0 ? tokens : [cleaned];
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
    { label: "Estimated Size", value: gbValue(item.estimated_space_gb) },
    { label: "Flagged", value: formatDate(item.created_at) },
    { label: "Season", value: textValue(item.season_number) },
  ];

  const rules = $derived(parseRuleTokens(entry.reason));
</script>

<div class="space-y-3">
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
