export type DiagnosticsScope =
  "movie" | "movie_version" | "series" | "season" | "episode";
export interface DiagnosticsTarget {
  scope: DiagnosticsScope;
  id: number;
}
export interface DiagnosticsReport {
  report_id: string;
  report_kind: "system" | "media";
  generated_at: string;
  summary: string[];
  target: DiagnosticsTarget | null;
  sections: {
    decisions: DecisionContext | null;
    services:
      { id: number; alias: string; service: string; enabled: boolean }[] | null;
    children:
      | (DiagnosticsTarget & {
          title: string | null;
          season_number: number | null;
          episode_number: number | null;
          size: number | null;
          video_width: number | null;
          video_height: number | null;
        })[]
      | null;
  };
  collection: {
    children_offset: number;
    children_total: number | null;
    partial: boolean;
    observations: { section: string; state: string; reason: string | null }[];
    truncated: { section: string; returned: number; total: number | null }[];
    omitted: string[];
  };
}

export type BundleSection =
  | "services"
  | "configuration"
  | "storage"
  | "playback"
  | "decisions"
  | "tasks"
  | "timeline";
export type BundlePreset =
  "playback" | "candidate" | "deletion" | "paths" | "system";
export interface BundlePreview {
  download_token: string;
  expires_at: string;
  archive_bytes: number;
  sha256: string;
  manifest: {
    report_id: string;
    inventory: { section: string; records: number }[];
    files: string[];
    excluded: string[];
    limitations: string[];
    window_start: string;
    window_end: string;
    file_paths_included: boolean;
    restorable: false;
  };
  report: DiagnosticsReport;
  traces: DecisionTrace[];
  timeline: TimelineEntry[];
  logs: unknown[];
}

export interface RecordedCondition {
  field: string;
  field_label?: string;
  operator: string | null;
  operator_label?: string;
  actual: unknown;
  expected: unknown;
}
export interface TraceCondition extends RecordedCondition {
  availability: "available" | "unknown" | "not_evaluated";
  outcome: "matched" | "not_matched" | "unknown" | "not_evaluated";
}
export interface ProtectionEvidence {
  id: number;
  permanent: boolean;
  expires_at: string | null;
  source_rule_id: number | null;
}
export interface DecisionContext {
  basis: string;
  candidates: {
    id: number;
    target: DiagnosticsTarget;
    recorded_at: string | null;
    matched_rule_ids: number[];
    conditions: RecordedCondition[];
    conditions_state: string;
  }[];
  protections: (ProtectionEvidence & { target: DiagnosticsTarget })[];
}
export interface DecisionTrace {
  id: number;
  scan_id: string;
  job_id: number | null;
  target: DiagnosticsTarget;
  candidate_id: number | null;
  rule_id: number;
  rule_revision: string;
  definition: unknown;
  observed_at: string;
  outcome: "matched" | "not_matched" | "unknown";
  conditions: TraceCondition[];
  protection_checks: {
    basis: string;
    protections: ProtectionEvidence[];
    truncated: boolean;
  };
  scan_context: {
    status?: string;
    skipped_rule_ids?: number[];
    dropped_traces?: number;
    favorites_refresh_available?: boolean;
    seerr_refresh_available?: boolean;
    playback_blocking?: boolean;
    playback_unavailable_targets?: number;
    sonarr_unavailable_series?: number;
  };
  note: string;
}
export interface TimelineEntry {
  occurred_at: string;
  event: string;
  correlation_id: string;
  candidate_id: number | null;
  job_id: number | null;
  target: DiagnosticsTarget | null;
  error_category: string | null;
  note: string | null;
  operation: {
    action: string;
    stage: string;
    outcome: string;
    subject: string;
    path: string | null;
    destination: string | null;
    provider: string | null;
    reference: string | null;
    shared_request: boolean;
    dropped_observations: number;
  } | null;
}
export interface SupportMatch {
  code: string;
  kind: "scan" | "operation" | "job";
  occurred_at: string | null;
  targets: DiagnosticsTarget[];
  targets_truncated: boolean;
  suggested: {
    support_code: string;
    target: DiagnosticsTarget | null;
    preset: BundlePreset;
    sections: BundleSection[];
    history_days: number;
  };
}
export interface SupportLookupResult {
  state: "found" | "not_found" | "unavailable";
  matches: SupportMatch[];
  limitations: string[];
}
export type CheckKind = "path" | "provider" | "relationships";
export interface DiagnosticCheckResult {
  check_id: string;
  kind: CheckKind;
  checked_at: string;
  observations: {
    subject: string;
    status: "passed" | "failed" | "unknown";
    detail: string;
  }[];
  note: string;
}
