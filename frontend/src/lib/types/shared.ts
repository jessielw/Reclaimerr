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

export interface AccountSession {
  session_id: string;
  user_agent: string | null;
  ip_address: string | null;
  created_at: string;
  last_seen_at: string | null;
  expires_at: string;
  is_current: boolean;
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
  Tautulli = "tautulli",
  General = "general",
  Authentication = "authentication",
  UserSignals = "user_signals",
  Tasks = "tasks",
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
  preferences: NotificationPreferences;
}

export interface NotificationTypePreference {
  detail: string;
  max_items?: number;
}

export type NotificationPreferences = Record<
  string,
  NotificationTypePreference
>;

export enum NotificationType {
  NewCleanupCandidates = "new_cleanup_candidates",
  RequestApproved = "request_approved",
  RequestDeclined = "request_declined",
  AdminMessage = "admin_message",
  TaskFailure = "task_failure",
}

export interface PathMapping {
  source_prefix: string;
  local_prefix: string;
  service_type?: string | null;
  service_config_id?: number | null;
}

export interface PostActionWebhookHeader {
  name: string;
  value: string;
}

export interface PostActionWebhookConfig {
  enabled: boolean;
  name: string;
  method: "GET" | "POST";
  url_template: string;
  headers: PostActionWebhookHeader[];
  auth_username: string | null;
  auth_password: string | null;
  actions: ("deleted" | "moved")[];
  media_types: MediaType[];
  path_mode: "original" | "local" | "destination";
  body_template: string | null;
  timeout_seconds: number;
}

export interface RequesterWatchUserMapping {
  seerr_user_id: number | null;
  seerr_username: string | null;
  media_user_key: string;
  service_type: string | null;
}

export interface WatchUserLookup {
  user_key: string;
  user_key_normalized: string;
  source_services: string[];
}

export interface SeerrUserLookup {
  id: number;
  username: string | null;
  display_name: string | null;
}

export interface GeneralSettings {
  worker_poll_min_seconds: number | null;
  worker_poll_max_seconds: number | null;
  path_mappings: PathMapping[];
  post_action_webhooks: PostActionWebhookConfig[];
  move_enabled: boolean;
  move_destination_movies: string | null;
  move_destination_series: string | null;
  media_server_fallback_enabled: boolean;
  default_arr_delete_behavior: "unmonitor" | "remove_if_empty";
  add_arr_import_exclusions_on_delete: boolean;
  favorites_ignore_enabled: boolean;
  favorites_protect_all_users: boolean;
  favorites_usernames: string[];
  requester_watch_user_mappings: RequesterWatchUserMapping[];
  leaving_soon_enabled: boolean;
  leaving_soon_collection_title: string;
}

export interface OIDCSettings {
  enabled: boolean;
  issuer_url: string;
  client_id: string;
  scopes: string;
  email_claim: string;
  token_endpoint_auth_method: "client_secret_basic" | "client_secret_post";
  redirect_uri_override: string | null;
  client_secret_configured: boolean;
}

export interface FavoritesUserLookup {
  username: string;
  has_favorites: boolean;
  favorites_count: number;
  source_count: number;
  sources: string[];
}

export interface FavoritesMediaEntry {
  media_type: MediaType;
  tmdb_id: number;
  title: string;
  year: number | null;
  poster_url: string | null;
  favorite_user_count: number;
  favorite_users: string[];
  is_missing_metadata: boolean;
}

export interface PaginatedFavoritesMediaResponse {
  items: FavoritesMediaEntry[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

export interface ReclaimRule {
  id: number;
  name: string;
  media_type: MediaType;
  enabled: boolean;
  target_scope: "movie_version" | "series" | "season" | "episode" | null;
  definition: RuleDefinition | null;
  action: RuleAction | null;
  created_at: string;
  updated_at: string;
}

export type RuleGroupOperator = "and" | "or";
export type RuleConditionOperator =
  | "equals"
  | "not_equals"
  | "greater_than"
  | "greater_than_or_equal"
  | "less_than"
  | "less_than_or_equal"
  | "before"
  | "on_or_before"
  | "after"
  | "on_or_after"
  | "in"
  | "not_in"
  | "contains_any"
  | "not_contains_any"
  | "exists"
  | "not_exists"
  | "is_true"
  | "is_false"
  | "matches_any_regex";

export interface RuleCondition {
  type: "condition";
  field: string;
  operator: RuleConditionOperator;
  value?: string | number | boolean | string[] | number[] | null;
}

export interface RuleGroup {
  type: "group";
  op: RuleGroupOperator;
  children: RuleNode[];
}

export type RuleNode = RuleCondition | RuleGroup;

export interface RuleDefinition {
  version: number;
  root: RuleGroup;
}

export interface RuleAction {
  candidate: boolean;
  tag_enabled: boolean;
  arr_tag: string | null;
  arr_action: "delete" | "unmonitor";
  media_server_action: "delete" | null;
  radarr_service_config_id: number | null;
  sonarr_service_config_id: number | null;
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
  CandidateFileOp = "candidate_file_op",
}

export type CandidateFileOpScope =
  | "movie"
  | "version"
  | "series"
  | "season"
  | "episode";

export interface CandidateFileOpJobItem {
  candidate_id: number;
  media_type: MediaType;
  scope: CandidateFileOpScope;
  title: string;
  year: number | null;
  tmdb_id: number | null;
  season_number: number | null;
  episode_number: number | null;
  episode_name: string | null;
  resolution: string | null;
  hdr: boolean | null;
  dolby_vision: boolean | null;
  display_label: string;
}

export interface CandidateFileOpJobProgress {
  total_items: number;
  completed_items: number;
  failed_items: number;
  current_item_label: string | null;
  percent: number;
}

export interface CandidateFileOpJobResult {
  operation: "delete" | "move";
  processed: number;
  succeeded: number;
  failed: number;
}

export interface CandidateFileOpJobPayload {
  operation: "delete" | "move";
  candidate_ids: number[];
  requested_by_user_id: number;
  requested_by_username: string;
  delete_request_id?: number | null;
  item_labels: string[];
  item_label_total?: number | null;
  item_details?: CandidateFileOpJobItem[];
  progress?: CandidateFileOpJobProgress | null;
  result?: CandidateFileOpJobResult | null;
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
  payload: Record<string, unknown> | CandidateFileOpJobPayload;
}

// media browsing types
export interface MediaStatusInfo {
  is_candidate: boolean;
  candidate_id: number | null;
  candidate_reason: string | null;
  candidate_space_bytes: number | null;
  is_protected: boolean;
  protected_reason: string | null;
  protected_permanent: boolean;
  has_pending_request: boolean;
  request_id: number | null;
  request_status: string | null;
  request_reason: string | null;
  has_pending_delete_request: boolean;
  delete_request_id: number | null;
  delete_request_status: string | null;
  delete_request_reason: string | null;
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
  arr_refs: ArrRef[];
  imdb_id: string | null;
  imdb_rating: number | null;
  imdb_vote_count: number | null;
  imdb_ratings_refreshed_at: string | null;
  anilist_id: number | null;
  anilist_score: number | null;
  anilist_popularity: number | null;
  anilist_favourites: number | null;
  anilist_refreshed_at: string | null;
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
  arr_refs: ArrRef[];
  imdb_id: string | null;
  imdb_rating: number | null;
  imdb_vote_count: number | null;
  imdb_ratings_refreshed_at: string | null;
  anilist_id: number | null;
  anilist_score: number | null;
  anilist_popularity: number | null;
  anilist_favourites: number | null;
  anilist_refreshed_at: string | null;
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
  has_season_candidates: boolean;
  library_season_count: number;
  library_episode_count: number;
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

export interface EpisodeWithStatus {
  id: number;
  season_id: number;
  season_number: number;
  episode_number: number;
  name: string | null;
  size: number | null;
  view_count: number;
  air_date: string | null;
  last_viewed_at: string | null;
  status: MediaStatusInfo;
}

export interface ArrRef {
  service_type: string;
  service_config_id: number;
  arr_id: number;
}

export type MediaItem = MovieWithStatus | SeriesWithStatus;

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

export interface SidebarIndicatorsResponse {
  has_candidates: boolean;
  has_pending_requests: boolean;
  has_pending_protection_requests: boolean;
  has_pending_delete_requests: boolean;
}

export interface UiIndicatorsResponse extends SidebarIndicatorsResponse {
  update_available: boolean;
  latest_version: string | null;
  latest_release_url: string | null;
  last_checked_at: string | null;
  has_unread_notices: boolean;
}

export interface AdminNotice {
  id: number;
  kind: string;
  severity: "warning" | "error" | string;
  title: string;
  message: string;
  action_label: string | null;
  action_href: string | null;
  is_read: boolean;
  is_active: boolean;
  read_at: string | null;
  last_occurred_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminNoticesResponse {
  unread_count: number;
  items: AdminNotice[];
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
  target_scope:
    | "movie"
    | "movie_version"
    | "series"
    | "season"
    | "episode"
    | null;
  movie_version_id: number | null;
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
  episode_id: number | null;
  episode_number: number | null;
  episode_name: string | null;
  created_at: string;
  updated_at: string;
  version_resolution: string | null;
  version_file_name: string | null;
  version_size: number | null;
  version_video_codec: string | null;
  version_hdr: boolean | null;
  version_dolby_vision: boolean | null;
  season_size: number | null;
  season_resolution: string | null;
  season_video_codecs: string[] | null;
  season_hdr: boolean | null;
  season_dolby_vision: boolean | null;
}

// delete requests
export interface DeleteRequest {
  id: number;
  media_type: MediaType;
  poster_url: string | null;
  media_id: number;
  target_scope:
    | "movie"
    | "movie_version"
    | "series"
    | "season"
    | "episode"
    | null;
  movie_version_id: number | null;
  media_title: string;
  media_year: number | null;
  requested_by_user_id: number;
  requested_by_username: string;
  reason: string | null;
  status: ProtectionRequestStatus;
  reviewed_by_user_id: number | null;
  reviewed_by_username: string | null;
  reviewed_at: string | null;
  admin_notes: string | null;
  executed_at: string | null;
  execution_error: string | null;
  season_id: number | null;
  season_number: number | null;
  episode_id: number | null;
  episode_number: number | null;
  episode_name: string | null;
  created_at: string;
  updated_at: string;
  version_resolution: string | null;
  version_file_name: string | null;
  version_size: number | null;
  version_video_codec: string | null;
  version_hdr: boolean | null;
  version_dolby_vision: boolean | null;
  season_size: number | null;
  season_resolution: string | null;
  season_video_codecs: string[] | null;
  season_hdr: boolean | null;
  season_dolby_vision: boolean | null;
}

export interface ProtectedEntry {
  id: number;
  media_type: MediaType;
  media_id: number;
  movie_version_id: number | null;
  season_id: number | null;
  season_number: number | null;
  episode_id: number | null;
  episode_number: number | null;
  episode_name: string | null;
  media_title: string;
  media_year: number | null;
  poster_url: string | null;
  tmdb_id: number | null;
  vote_average: number | null;
  vote_count: number | null;
  imdb_id: string | null;
  imdb_rating: number | null;
  imdb_vote_count: number | null;
  anilist_id: number | null;
  anilist_score: number | null;
  anilist_popularity: number | null;
  anilist_favourites: number | null;
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
  movie_version_id: number | null;
  media_title: string;
  media_year: number | null;
  poster_url: string | null;
  tmdb_id: number | null;
  imdb_id: string | null;
  imdb_rating: number | null;
  imdb_vote_count: number | null;
  anilist_id: number | null;
  anilist_score: number | null;
  anilist_popularity: number | null;
  anilist_favourites: number | null;
  genres: string[] | null;
  popularity: number | null;
  vote_average: number | null;
  vote_count: number | null;
  tmdb_status: string | null;
  version_service: string | null;
  version_library_id: string | null;
  version_library_name: string | null;
  version_video_codec_family: string | null;
  version_audio_codec_family: string | null;
  version_video_width: number | null;
  version_video_height: number | null;
  version_video_resolution: string | null;
  version_video_hdr: boolean | null;
  version_video_dolby_vision: boolean | null;
  version_audio_channels: number | null;
  version_audio_languages: string[] | null;
  version_size: number | null;
  version_path: string | null;
  version_file_name: string | null;
  version_subtitle_languages: string[] | null;
  reason_parts: {
    rule_id: number | null;
    rule_name: string;
    target_scope: string;
    season_label: string | null;
    conditions: {
      field: string;
      field_label: string;
      operator: string;
      operator_label: string;
      expected:
        | string
        | number
        | boolean
        | (string | number | boolean)[]
        | null;
      actual: string | number | boolean | (string | number | boolean)[] | null;
      display: string;
    }[];
    text: string;
  }[];
  reason_tokens: string[];
  estimated_space_bytes: number | null;
  has_pending_request: boolean;
  created_at: string;
  // populated for season level candidates
  season_id: number | null;
  season_number: number | null;
  series_title: string | null;
  season_has_hdr: boolean | null;
  season_has_dolby_vision: boolean | null;
  season_max_video_width: number | null;
  season_max_video_height: number | null;
  season_video_codec_families: string[] | null;
  season_audio_codec_families: string[] | null;
  season_audio_languages: string[] | null;
  season_subtitle_languages: string[] | null;
  // populated for episode level candidates
  episode_id: number | null;
  episode_number: number | null;
  episode_name: string | null;
  series_library_refs:
    | {
        library_id: string;
        library_name: string;
        service: string | null;
      }[]
    | null;
}

export type RulePreviewEntry = Omit<
  ReclaimCandidateEntry,
  "id" | "has_pending_request" | "created_at"
>;

export interface DashboardKpis {
  total_movies: number;
  total_series: number;
  total_movies_size_bytes: number;
  total_series_size_bytes: number;
  reclaimable_movies_bytes: number;
  reclaimable_series_bytes: number;
  reclaimable_total_bytes: number;
  reclaimed_movies: number;
  reclaimed_series: number;
  reclaimed_total_bytes: number;
}

export interface ReclaimHistoryEntry {
  id: number;
  approved_by: string;
  media_type: string;
  tmdb_id: number | null;
  name: string | null;
  size: number | null;
  attributes: {
    resolution: string | null;
    hdr: boolean | null;
    dolby_vision: boolean | null;
  } | null;
  action: string;
  destination_path: string | null;
  created_at: string;
}

export interface DashboardRequestsSummary {
  pending_count: number;
  approved_7d: number;
  denied_7d: number;
  mine_pending: number;
  mine_active: number;
}

export interface DashboardServiceSummary {
  service_type: string;
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
