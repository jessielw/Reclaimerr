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

export enum Permission {
  ManageUsers = "manage_users",
  ManageRequests = "manage_requests",
  Request = "request",
  AutoApprove = "auto_approve",
  ManageBlacklist = "manage_blacklist",
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
  Jellyfin = "jellyfin",
  Plex = "plex",
  Radarr = "radarr",
  Sonarr = "sonarr",
  Seerr = "seerr",
  General = "general",
  Tasks = "tasks",
  Notifications = "notifications",
  Account = "account",
  Rules = "rules",
  Users = "users",
  About = "about",
}

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
  auto_tag: boolean;
  created_at: string;
  updated_at: string;
}

export enum ScheduleType {
  Cron = "cron",
  Interval = "interval",
}

export enum TaskStatus {
  Scheduled = "scheduled",
  Completed = "completed",
  Error = "error",
  Running = "running",
  Disabled = "disabled",
}

// media browsing types
export interface MediaStatusInfo {
  is_candidate: boolean;
  candidate_id: number | null;
  candidate_reason: string | null;
  candidate_space_gb: number | null;
  is_blacklisted: boolean;
  blacklist_reason: string | null;
  blacklist_permanent: boolean;
  has_pending_request: boolean;
  request_id: number | null;
  request_status: string | null;
  request_reason: string | null;
}

export interface MovieWithStatus {
  id: number;
  title: string;
  year: number;
  tmdb_id: number;
  size: number | null;
  plex_path: string | null;
  jellyfin_path: string | null;
  plex_library_name: string | null;
  jellyfin_library_name: string | null;
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
  never_watched: boolean;
  status: MediaStatusInfo;
  added_at: string | null;
}

export interface SeriesWithStatus {
  id: number;
  title: string;
  year: number;
  tmdb_id: number;
  size: number | null;
  plex_path: string | null;
  jellyfin_path: string | null;
  plex_library_name: string | null;
  jellyfin_library_name: string | null;
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
  never_watched: boolean;
  status: MediaStatusInfo;
  added_at: string | null;
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
export enum ExceptionRequestStatus {
  Pending = "pending",
  Approved = "approved",
  Denied = "denied",
}

// exception request type used for both movie and series requests
export interface ExceptionRequest {
  id: number;
  media_type: MediaType;
  poster_url: string | null;
  media_id: number;
  media_title: string;
  media_year: number;
  candidate_id: number | null;
  requested_by_user_id: number;
  requested_by_username: string;
  reason: string;
  requested_expires_at: string | null;
  status: ExceptionRequestStatus;
  reviewed_by_user_id: number | null;
  reviewed_by_username: string | null;
  reviewed_at: string | null;
  admin_notes: string | null;
  effective_permanent: boolean | null;
  effective_expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface BlacklistEntry {
  id: number;
  media_type: MediaType;
  media_id: number;
  media_title: string;
  media_year: number;
  poster_url: string | null;
  reason: string | null;
  blacklisted_by_user_id: number;
  blacklisted_by_username: string;
  permanent: boolean;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DashboardKpis {
  total_movies: number;
  total_series: number;
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
  status: string;
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
}
