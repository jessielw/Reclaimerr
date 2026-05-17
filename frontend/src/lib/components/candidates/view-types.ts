import type { ReclaimCandidateEntry } from "$lib/types/shared";

export type FlatRow = { kind: "flat"; entry: ReclaimCandidateEntry };

export type SeriesGroupRow = {
  kind: "group";
  group_type: "series_seasons";
  seriesEntry: ReclaimCandidateEntry | null;
  seasons: ReclaimCandidateEntry[];
  versions: ReclaimCandidateEntry[];
  media_id: number;
  media_title: string;
  media_year: number | null;
  poster_url: string | null;
};

export type MovieGroupRow = {
  kind: "group";
  group_type: "movie_versions";
  seriesEntry: ReclaimCandidateEntry | null;
  seasons: ReclaimCandidateEntry[];
  versions: ReclaimCandidateEntry[];
  media_id: number;
  media_title: string;
  media_year: number | null;
  poster_url: string | null;
};

export type GroupRow = SeriesGroupRow | MovieGroupRow;
export type DisplayRow = FlatRow | GroupRow;
