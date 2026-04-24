export enum UserRole {
  Admin = "admin",
  User = "user",
}

export interface User {
  id: number;
  username: string;
  display_name: string | null;
  email: string | null;
  role: UserRole;
  permissions: Permission[];
}

export enum AlertLevel {
  // INFO = "info", // reserved for future use if we want non admin alerts
  WARNING = "warning",
  ERROR = "error",
}

export enum Permission {
  ManageUsers = "manage_users",
  ManageRequests = "manage_requests",
  Request = "request",
  AutoApprove = "auto_approve",
  ManageProtection = "manage_protection",
  ManageReclaim = "manage_reclaim",
}

export interface UserProfile extends User {
  avatar_url: string | null;
  created_at: string;
}

export enum MediaType {
  Movie = "movie",
  Series = "series",
}

export enum SettingsTab {
  MediaServers = "media_servers",
  Jellyfin = "jellyfin",
  Emby = "emby",
  Plex = "plex",
  Radarr = "radarr",
  Sonarr = "sonarr",
  Seerr = "seerr",
  General = "general",
  Tasks = "tasks",
  BackgroundJobs = "background_jobs",
  Notifications = "notifications",
  Account = "account",
  Rules = "rules",
  Users = "users",
  About = "about",
}

export const MEDIA_SERVERS = [
  SettingsTab.Jellyfin,
  SettingsTab.Emby,
  SettingsTab.Plex,
] as const;

export type LibraryType = {
  id: number;
  libraryId: string;
  libraryName: string;
  mediaType: MediaType;
  serviceType: SettingsTab;
  selected: boolean;
};

export interface NotificationSetting {
  id: number;
  enabled: boolean;
  name: string | null;
  url: string;
  newCleanupCandidates: boolean;
  requestApproved: boolean;
  requestDeclined: boolean;
  adminMessage: boolean;
  taskFailure: boolean;
}

export enum NotificationType {
  NewCleanupCandidates = "new_cleanup_candidates",
  RequestApproved = "request_approved",
  RequestDeclined = "request_declined",
  AdminMessage = "admin_message",
  TaskFailure = "task_failure",
}

export interface GeneralSettings {
  auto_tag_enabled: boolean;
  cleanup_tag_suffix: string;
  worker_poll_min_seconds: number | null;
  worker_poll_max_seconds: number | null;
}

export interface ReclaimRule {
  id: number;
  name: string;
  media_type: MediaType;
  enabled: boolean;
  library_ids: string[] | null;
  min_popularity: number | null;
  max_popularity: number | null;
  min_vote_average: number | null;
  max_vote_average: number | null;
  min_vote_count: number | null;
  max_vote_count: number | null;
  min_view_count: number | null;
  max_view_count: number | null;
  include_never_watched: boolean;
  min_days_since_added: number | null;
  max_days_since_added: number | null;
  min_days_since_last_watched: number | null;
  max_days_since_last_watched: number | null;
  min_size: number | null;
  max_size: number | null;
  paths: string[] | null;
  series_status: string[] | null;
  created_at: string;
  updated_at: string;
}

export enum ScheduleType {
  Cron = "cron",
  Interval = "interval",
}

export enum TaskStatus {
  Scheduled = "scheduled",
  Queued = "queued",
  Completed = "completed",
  Error = "error",
  Running = "running",
  Disabled = "disabled",
}

export enum BackgroundJobStatus {
  Pending = "pending",
  Running = "running",
  Completed = "completed",
  Failed = "failed",
  Canceled = "canceled",
}

export enum BackgroundJobType {
  ServiceToggle = "service_toggle",
  TaskRun = "task_run",
}

export interface BackgroundJobRecord {
  id: number;
  job_type: BackgroundJobType;
  status: BackgroundJobStatus;
  summary: string | null;
  dedupe_key: string | null;
  attempts: number;
  max_attempts: number;
  claimed_by: string | null;
  claimed_at: string | null;
  scheduled_at: string;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  payload: Record<string, unknown>;
}

// media browsing types
export interface MediaStatusInfo {
  is_candidate: boolean;
  candidate_id: number | null;
  candidate_reason: string | null;
  candidate_space_gb: number | null;
  is_protected: boolean;
  protected_reason: string | null;
  protected_permanent: boolean;
  has_pending_request: boolean;
  request_id: number | null;
  request_status: string | null;
  request_reason: string | null;
}

export interface MovieVersion {
  id: number;
  service: string;
  service_item_id: string;
  service_media_id: string;
  library_id: string;
  library_name: string;
  path: string | null;
  size: number;
  added_at: string | null;
  container: string | null;
}

export interface MovieWithStatus {
  id: number;
  title: string;
  year: number | null;
  tmdb_id: number;
  size: number | null;
  versions: MovieVersion[];
  radarr_id: number | null;
  imdb_id: string | null;
  tmdb_title: string | null;
  original_title: string | null;
  tmdb_release_date: string | null;
  original_language: string | null;
  poster_url: string | null;
  backdrop_url: string | null;
  overview: string | null;
  genres: string[] | null;
  popularity: number | null;
  vote_average: number | null;
  vote_count: number | null;
  runtime: number | null;
  tagline: string | null;
  last_viewed_at: string | null;
  view_count: number;
  status: MediaStatusInfo;
  added_at: string | null;
}

export interface SeriesServiceRef {
  service: string;
  service_id: string;
  library_id: string;
  library_name: string;
  path: string | null;
}

export interface SeriesWithStatus {
  id: number;
  title: string;
  year: number | null;
  tmdb_id: number;
  size: number | null;
  service_refs: SeriesServiceRef[];
  sonarr_id: number | null;
  imdb_id: string | null;
  tvdb_id: string | null;
  tmdb_title: string | null;
  original_title: string | null;
  tmdb_first_air_date: string | null;
  tmdb_last_air_date: string | null;
  original_language: string | null;
  poster_url: string | null;
  backdrop_url: string | null;
  overview: string | null;
  genres: string[] | null;
  popularity: number | null;
  vote_average: number | null;
  vote_count: number | null;
  season_count: number | null;
  tagline: string | null;
  last_viewed_at: string | null;
  view_count: number;
  status: MediaStatusInfo;
  added_at: string | null;
}

export interface SeasonWithStatus {
  id: number;
  season_number: number;
  episode_count: number | null;
  size: number | null; // bytes
  view_count: number;
  last_viewed_at: string | null;
  air_date: string | null;
  status: MediaStatusInfo;
}

export type MediaItem = MovieWithStatus | SeriesWithStatus;

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

// exception requests
export enum ProtectionRequestStatus {
  Pending = "pending",
  Approved = "approved",
  Denied = "denied",
}

// exception request type used for both movie and series requests
export interface ProtectionRequest {
  id: number;
  media_type: MediaType;
  poster_url: string | null;
  media_id: number;
  media_title: string;
  media_year: number | null;
  candidate_id: number | null;
  requested_by_user_id: number;
  requested_by_username: string;
  reason: string;
  requested_expires_at: string | null;
  status: ProtectionRequestStatus;
  reviewed_by_user_id: number | null;
  reviewed_by_username: string | null;
  reviewed_at: string | null;
  admin_notes: string | null;
  effective_permanent: boolean | null;
  effective_expires_at: string | null;
  season_id: number | null;
  season_number: number | null;
  created_at: string;
  updated_at: string;
}

export interface ProtectedEntry {
  id: number;
  media_type: MediaType;
  media_id: number;
  media_title: string;
  media_year: number | null;
  poster_url: string | null;
  reason: string | null;
  protected_by_user_id: number;
  protected_by_username: string;
  permanent: boolean;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ReclaimCandidateEntry {
  id: number;
  media_type: MediaType;
  media_id: number;
  media_title: string;
  media_year: number | null;
  poster_url: string | null;
  reason: string;
  estimated_space_gb: number | null;
  has_pending_request: boolean;
  created_at: string;
  // populated for season-level candidates
  season_id: number | null;
  season_number: number | null;
  series_title: string | null;
}

export interface DashboardKpis {
  total_movies: number;
  total_series: number;
  total_movies_size_gb: number;
  total_series_size_gb: number;
  reclaimable_movies_gb: number;
  reclaimable_series_gb: number;
  reclaimable_total_gb: number;
}

export interface DashboardRequestsSummary {
  pending_count: number;
  approved_7d: number;
  denied_7d: number;
  mine_pending: number;
  mine_active: number;
}

export interface DashboardServiceSummary {
  name: string;
  url: string;
  enabled: boolean;
  last_sync_at: string | null;
}

export interface DashboardActivityItem {
  id: string;
  type: string;
  title: string;
  subtitle: string | null;
  created_at: string;
  actor_display: string | null;
  media_type: string | null;
  media_title: string | null;
}

export interface DashboardViewer {
  role: UserRole;
  can_view_admin_panels: boolean;
}

export interface DashboardResponse {
  kpis: DashboardKpis;
  requests: DashboardRequestsSummary;
  services: DashboardServiceSummary[];
  activity: DashboardActivityItem[];
  viewer: DashboardViewer;
  media_server_configured: boolean;
}
