import { formatFileSize, cleanResolutionString } from "$lib/utils/formatters";
import { fileNameFromPath } from "$lib/utils/candidate-rules";
import type { ReclaimCandidateEntry } from "$lib/types/shared";

export const UNKNOWN_VALUE = "Unknown";

const candidateCreatedAtEpoch = (createdAt: string): number => {
  const hasTimezone = /[zZ]|[+-]\d{2}:\d{2}$/.test(createdAt);
  const parsed = Date.parse(hasTimezone ? createdAt : `${createdAt}Z`);
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
};

export const newestCandidateCreatedAt = (
  entries: ReclaimCandidateEntry[],
): string | null => {
  if (entries.length === 0) return null;

  let newest = entries[0].created_at;
  let newestEpoch = candidateCreatedAtEpoch(newest);

  for (const entry of entries) {
    const epoch = candidateCreatedAtEpoch(entry.created_at);
    if (epoch > newestEpoch) {
      newest = entry.created_at;
      newestEpoch = epoch;
    }
  }

  return newest;
};

export const movieSummaryChips = (entry: ReclaimCandidateEntry): string[] => {
  const chips: string[] = [];
  if (entry.version_video_width && entry.version_video_height) {
    chips.push(`${entry.version_video_width}x${entry.version_video_height}`);
  }
  if (entry.version_video_codec_family) {
    chips.push(entry.version_video_codec_family.toUpperCase());
  }
  if (entry.version_video_dolby_vision) chips.push("DV");
  else if (entry.version_video_hdr) chips.push("HDR");
  if (entry.version_audio_codec_family) {
    chips.push(entry.version_audio_codec_family.toUpperCase());
  }
  chips.push(formatFileSize(entry.estimated_space_bytes));
  return chips;
};

export const versionResolutionLabel = (
  entry: ReclaimCandidateEntry,
): string => {
  const res =
    entry.version_video_resolution ||
    (entry.version_video_height ? String(entry.version_video_height) : null);
  return cleanResolutionString(res) ?? UNKNOWN_VALUE;
};

export const seasonResolutionLabel = (entry: ReclaimCandidateEntry): string => {
  const res = entry.season_max_video_height
    ? String(entry.season_max_video_height)
    : null;
  return cleanResolutionString(res) ?? UNKNOWN_VALUE;
};

export const candidateFileName = (
  path: string | null,
  fallbackFileName: string | null = null,
): string => fileNameFromPath(path, fallbackFileName);

export const seriesGroupCountLabel = (
  entries: ReclaimCandidateEntry[],
): string => {
  const seasonCount = entries.filter((s) => s.episode_number == null).length;
  const episodeCount = entries.filter((s) => s.episode_number != null).length;
  return [
    seasonCount > 0
      ? `${seasonCount} season${seasonCount !== 1 ? "s" : ""}`
      : "",
    episodeCount > 0
      ? `${episodeCount} episode${episodeCount !== 1 ? "s" : ""}`
      : "",
  ]
    .filter(Boolean)
    .join(", ");
};

export const groupEpisodesBySeason = (
  entries: ReclaimCandidateEntry[],
): [number, ReclaimCandidateEntry[]][] => {
  const bySeason = new Map<number, ReclaimCandidateEntry[]>();
  for (const ep of entries) {
    const key = ep.season_number ?? 0;
    const current = bySeason.get(key) ?? [];
    current.push(ep);
    bySeason.set(key, current);
  }
  return [...bySeason.entries()].sort((a, b) => a[0] - b[0]);
};
