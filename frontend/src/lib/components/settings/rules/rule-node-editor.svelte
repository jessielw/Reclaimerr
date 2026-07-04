<script lang="ts" module>
  const RULE_DND_CONTEXT = Symbol("rule-dnd-context");
</script>

<script lang="ts">
  import { getContext, setContext, untrack } from "svelte";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Command from "$lib/components/ui/command/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import * as Popover from "$lib/components/ui/popover/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import PathPatternPicker from "$lib/components/settings/rules/path-pattern-picker.svelte";
  import SeerrUserPicker from "$lib/components/settings/rules/seerr-user-picker.svelte";
  import PlaybackUserPicker from "$lib/components/settings/rules/playback-user-picker.svelte";
  import MovieCollectionPicker from "$lib/components/settings/rules/movie-collection-picker.svelte";
  import GenrePicker from "$lib/components/settings/rules/genre-picker.svelte";
  import MediaServerCollectionPicker from "$lib/components/settings/rules/media-server-collection-picker.svelte";
  import MetadataValuePicker from "$lib/components/settings/rules/metadata-value-picker.svelte";
  import FolderSearch from "@lucide/svelte/icons/folder-search";
  import ChevronsUpDown from "@lucide/svelte/icons/chevrons-up-down";
  import GripVertical from "@lucide/svelte/icons/grip-vertical";
  import Plus from "@lucide/svelte/icons/plus";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import Users from "@lucide/svelte/icons/users";
  import Self from "$lib/components/settings/rules/rule-node-editor.svelte";
  import { canMoveRuleNodeToGroup } from "$lib/components/settings/rules/rule-tree-dnd.js";
  import {
    dragHandle,
    dragHandleZone,
    SHADOW_ITEM_MARKER_PROPERTY_NAME,
    SHADOW_PLACEHOLDER_ITEM_ID,
    TRIGGERS,
    type DndEvent,
    type Options as DndOptions,
  } from "svelte-dnd-action";
  import type {
    MediaType,
    RuleCondition,
    RuleConditionOperator,
    RuleGroup,
    RuleNode,
  } from "$lib/types/shared";

  interface Props {
    node: RuleNode;
    rootNode?: RuleNode;
    depth?: number;
    targetScope?: RuleTargetScope;
    pathPickerMediaType?: MediaType;
    pathPickerLibraryIds?: string[] | null;
    onChange: () => void;
    onRemove?: () => void;
  }

  type RuleTargetScope = "movie_version" | "series" | "season" | "episode";
  type FieldKind = "number" | "text" | "boolean" | "temporal";

  interface FieldConfig {
    value: string;
    label: string;
    kind: FieldKind;
    operators: RuleConditionOperator[];
    defaultOperator: RuleConditionOperator;
  }

  interface FieldGroup {
    key: string;
    label: string;
    items: FieldConfig[];
  }

  interface RuleDndItem {
    id: string;
    node: RuleNode;
    isDndShadowItem?: boolean;
  }

  interface RuleDragState {
    ids: WeakMap<RuleNode, string>;
    nodesById: Map<string, RuleNode>;
    nextId: number;
    activeNode: RuleNode | null;
    activeSourceGroup: RuleGroup | null;
    clearScheduled: boolean;
  }

  let {
    node,
    rootNode = node,
    depth = 0,
    targetScope = "movie_version",
    pathPickerMediaType = undefined,
    pathPickerLibraryIds = null,
    onChange,
    onRemove,
  }: Props = $props();

  let pathPickerOpen = $state(false);
  let fieldPickerOpen = $state(false);
  let fieldQuery = $state("");
  let seerrPickerOpen = $state(false);
  let playbackUserPickerOpen = $state(false);
  let collectionPickerOpen = $state(false);
  let genrePickerOpen = $state(false);
  let mediaServerCollectionPickerOpen = $state(false);
  let originalLanguagePickerOpen = $state(false);
  let originCountryPickerOpen = $state(false);

  const MAX_TOTAL_GROUPS = 10;
  const MAX_GROUP_DEPTH = 4;
  const RULE_DND_TYPE = "reclaimerr-rule-node";

  const inheritedDragState = getContext<RuleDragState>(RULE_DND_CONTEXT);
  const localDragState = $state<RuleDragState>({
    ids: new WeakMap(),
    nodesById: new Map(),
    nextId: 0,
    activeNode: null,
    activeSourceGroup: null,
    clearScheduled: false,
  });
  const dragState = inheritedDragState ?? localDragState;
  if (!inheritedDragState) {
    setContext(RULE_DND_CONTEXT, dragState);
  }

  const getNodeId = (ruleNode: RuleNode): string => {
    const existing = dragState.ids.get(ruleNode);
    if (existing) return existing;

    dragState.nextId += 1;
    const id = `rule-node-${dragState.nextId}`;
    dragState.ids.set(ruleNode, id);
    dragState.nodesById.set(id, ruleNode);
    return id;
  };

  const toDndItems = (children: RuleNode[]): RuleDndItem[] =>
    children.map((child) => ({
      id: getNodeId(child),
      node: child,
    }));

  const isShadowItem = (item: RuleDndItem): boolean =>
    item.id === SHADOW_PLACEHOLDER_ITEM_ID ||
    item[SHADOW_ITEM_MARKER_PROPERTY_NAME] === true;

  let dndItems = $state<RuleDndItem[]>([]);

  const operatorLabelMap: Record<RuleConditionOperator, string> = {
    equals: "is",
    not_equals: "is not",
    greater_than: "greater than",
    greater_than_or_equal: ">=",
    less_than: "less than",
    less_than_or_equal: "<=",
    before: "before",
    on_or_before: "on or before",
    after: "after",
    on_or_after: "on or after",
    in: "in any",
    not_in: "not in any",
    contains_any: "matches any",
    not_contains_any: "matches none",
    contains_all: "matches all",
    not_contains_all: "does not match all",
    exists: "exists",
    not_exists: "does not exist",
    is_true: "is true",
    is_false: "is false",
    matches_any_regex: "matches regex",
  };

  const listOperators = new Set<RuleConditionOperator>([
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "contains_all",
    "not_contains_all",
    "matches_any_regex",
  ]);

  const valuelessOperators = new Set<RuleConditionOperator>([
    "exists",
    "not_exists",
    "is_true",
    "is_false",
  ]);

  const numericOperators: RuleConditionOperator[] = [
    "equals",
    "not_equals",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "exists",
    "not_exists",
  ];

  const textOperators: RuleConditionOperator[] = [
    "equals",
    "not_equals",
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "contains_all",
    "not_contains_all",
    "exists",
    "not_exists",
  ];

  const multiValueTextOperators: RuleConditionOperator[] = [
    "contains_any",
    "not_contains_any",
    "contains_all",
    "not_contains_all",
    "exists",
    "not_exists",
  ];

  const libraryOperators: RuleConditionOperator[] = [
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "contains_all",
    "not_contains_all",
    "exists",
    "not_exists",
  ];

  const pathOperators: RuleConditionOperator[] = [
    ...textOperators,
    "matches_any_regex",
  ];

  const booleanOperators: RuleConditionOperator[] = [
    "is_true",
    "is_false",
    "exists",
    "not_exists",
  ];
  const playbackActivityOperators: RuleConditionOperator[] = [
    "is_true",
    "is_false",
  ];

  const requesterIdOperators: RuleConditionOperator[] = [
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "contains_all",
    "not_contains_all",
    "exists",
    "not_exists",
  ];

  const temporalOperators: RuleConditionOperator[] = [
    "exists",
    "not_exists",
    "before",
    "on_or_before",
    "after",
    "on_or_after",
  ];

  const fields: FieldConfig[] = [
    {
      value: "library.id",
      label: "Library",
      kind: "text",
      operators: libraryOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "media.path",
      label: "Path",
      kind: "text",
      operators: pathOperators,
      defaultOperator: "matches_any_regex",
    },
    {
      value: "media.file_name",
      label: "Filename",
      kind: "text",
      operators: pathOperators,
      defaultOperator: "matches_any_regex",
    },
    {
      value: "media.size",
      label: "Size (bytes)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than",
    },
    {
      value: "media.year",
      label: "Year",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "equals",
    },
    {
      value: "media.container",
      label: "Container",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "media.days_since_added",
      label: "Days since added",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "arr.days_since_file_added",
      label: "Days since latest Arr file added",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "watch.view_count",
      label: "View count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "watch.days_since_last_watched",
      label: "Days since watched",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "watch.last_viewed_at",
      label: "Last watched",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "playback.has_activity",
      label: "Imported playback activity",
      kind: "boolean",
      operators: playbackActivityOperators,
      defaultOperator: "is_true",
    },
    {
      value: "playback.play_count",
      label: "Playback plays",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "playback.total_duration_minutes",
      label: "Playback duration (minutes)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "playback.longest_duration_minutes",
      label: "Longest playback (minutes)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "playback.unique_user_count",
      label: "Playback user count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "playback.usernames",
      label: "Playback users",
      kind: "text",
      operators: multiValueTextOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "playback.last_activity_at",
      label: "Last playback activity",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "playback.days_since_last_activity",
      label: "Days since playback activity",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "tmdb.release_date",
      label: "TMDB release date",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "tmdb.in_collection",
      label: "TMDB in collection",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "tmdb.collection_name",
      label: "TMDB collection name",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "tmdb.genres",
      label: "TMDB genres",
      kind: "text",
      operators: multiValueTextOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "tmdb.original_language",
      label: "TMDB original language",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "tmdb.origin_country",
      label: "TMDB origin country",
      kind: "text",
      operators: multiValueTextOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "tmdb.runtime_minutes",
      label: "TMDB runtime (minutes)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "tmdb.first_air_date",
      label: "TMDB first air date",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "tmdb.last_air_date",
      label: "TMDB last air date",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "season.air_date",
      label: "Season air date",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "tmdb.days_since_release",
      label: "Days since released",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "tmdb.days_since_first_air_date",
      label: "Days since first aired",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "tmdb.days_since_last_air_date",
      label: "Days since last aired",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "season.days_since_air_date",
      label: "Days since season aired",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "season.season_number",
      label: "Season number",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "equals",
    },
    {
      value: "season.episode_count",
      label: "Episode count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "less_than",
    },
    {
      value: "season.fully_watched",
      label: "Season fully watched",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "season.watched_percent",
      label: "Season watched (%)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "season.is_latest_season",
      label: "Is latest season",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "season.seasons_from_latest",
      label: "Seasons from latest",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "episode.number",
      label: "Episode number",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "equals",
    },
    {
      value: "episode.season_number",
      label: "Episode season number",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "equals",
    },
    {
      value: "episode.air_date",
      label: "Episode air date",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "before",
    },
    {
      value: "episode.days_since_air_date",
      label: "Days since episode aired",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "watch.never_watched",
      label: "Never watched",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "tmdb.popularity",
      label: "TMDB popularity",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "tmdb.vote_average",
      label: "TMDB rating",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "tmdb.vote_count",
      label: "TMDB votes",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "imdb.rating",
      label: "IMDb rating",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "imdb.vote_count",
      label: "IMDb votes",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "rottentomatoes.tomato_meter",
      label: "Rotten Tomatoes Tomatometer",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "rottentomatoes.tomato_vote_count",
      label: "Rotten Tomatoes Tomatometer votes",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "rottentomatoes.popcorn_meter",
      label: "Rotten Tomatoes Popcornmeter",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "rottentomatoes.popcorn_vote_count",
      label: "Rotten Tomatoes Popcornmeter votes",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "metacritic.metascore",
      label: "Metacritic metascore",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "metacritic.vote_count",
      label: "Metacritic critic count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "metacritic.user_score",
      label: "Metacritic user score",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "metacritic.user_vote_count",
      label: "Metacritic user votes",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "trakt.rating",
      label: "Trakt rating",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "trakt.vote_count",
      label: "Trakt votes",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "letterboxd.score",
      label: "Letterboxd score",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "letterboxd.vote_count",
      label: "Letterboxd votes",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "anilist.score",
      label: "AniList score",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "anilist.popularity",
      label: "AniList popularity",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "anilist.favourites",
      label: "AniList favourites",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "series.status",
      label: "Series status",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "series.tmdb_season_count",
      label: "TMDB season count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "series.library_season_count",
      label: "Library season count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "movie.version_count",
      label: "Movie version count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "video.codec_family",
      label: "Video codec family",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "audio.codec_family",
      label: "Audio codec family",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "video.hdr",
      label: "HDR",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "video.dolby_vision",
      label: "Dolby Vision",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "video.width",
      label: "Video width",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "video.height",
      label: "Video height",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "video.bitrate_kbps",
      label: "Video bitrate (kbps)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "video.bit_depth",
      label: "Video bit depth",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "video.resolution",
      label: "Resolution",
      kind: "text",
      operators: textOperators,
      defaultOperator: "equals",
    },
    {
      value: "audio.channels",
      label: "Audio channels",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "audio.track_count",
      label: "Audio track count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "audio.bitrate_kbps",
      label: "Audio bitrate (kbps)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "audio.languages",
      label: "Audio languages",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "subtitle.languages",
      label: "Subtitle languages",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "subtitle.track_count",
      label: "Subtitle track count",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "subtitle.has_forced",
      label: "Has forced subtitles",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "video.color_space",
      label: "Video color space",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "video.color_transfer",
      label: "Video color transfer",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "video.color_primaries",
      label: "Video color primaries",
      kind: "text",
      operators: textOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "media.duration",
      label: "Duration (ms)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "media_server.collections",
      label: "Media server collections",
      kind: "text",
      operators: multiValueTextOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "arr.tags",
      label: "Arr tags",
      kind: "text",
      operators: multiValueTextOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "arr.monitored",
      label: "Arr monitored",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "sonarr.latest_season_has_unaired_episodes",
      label: "Latest season has unaired episodes",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "sonarr.latest_season_has_finale",
      label: "Latest season has finale",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "seerr.requested",
      label: "Seerr requested",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "seerr.last_requested_at",
      label: "Seerr latest active request",
      kind: "temporal",
      operators: temporalOperators,
      defaultOperator: "exists",
    },
    {
      value: "seerr.days_since_last_requested",
      label: "Days since latest active Seerr request",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "greater_than_or_equal",
    },
    {
      value: "seerr.requester_has_watched",
      label: "Seerr requester has watched",
      kind: "boolean",
      operators: booleanOperators,
      defaultOperator: "is_true",
    },
    {
      value: "seerr.requested_by_user_ids",
      label: "Seerr requester IDs",
      kind: "text",
      operators: requesterIdOperators,
      defaultOperator: "contains_any",
    },
    {
      value: "disk.free_bytes",
      label: "Disk free (bytes)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "less_than",
    },
    {
      value: "disk.free_percent",
      label: "Disk free (%)",
      kind: "number",
      operators: numericOperators,
      defaultOperator: "less_than",
    },
  ];

  const PLAYBACK_FIELD_VALUES = [
    "playback.has_activity",
    "playback.play_count",
    "playback.total_duration_minutes",
    "playback.longest_duration_minutes",
    "playback.unique_user_count",
    "playback.usernames",
    "playback.last_activity_at",
    "playback.days_since_last_activity",
  ];

  const SCOPE_FIELD_VALUES: Record<RuleTargetScope, Set<string>> = {
    movie_version: new Set<string>([
      "anilist.favourites",
      "anilist.popularity",
      "anilist.score",
      "arr.monitored",
      "arr.tags",
      "arr.days_since_file_added",
      "audio.bitrate_kbps",
      "audio.channels",
      "audio.codec_family",
      "audio.languages",
      "audio.track_count",
      "disk.free_bytes",
      "disk.free_percent",
      "imdb.rating",
      "imdb.vote_count",
      "letterboxd.score",
      "letterboxd.vote_count",
      "metacritic.metascore",
      "metacritic.user_score",
      "metacritic.user_vote_count",
      "metacritic.vote_count",
      "library.id",
      "media.days_since_added",
      "media.container",
      "media.duration",
      "media.file_name",
      "media.path",
      "media.size",
      "media.year",
      "media_server.collections",
      "seerr.requested",
      "seerr.last_requested_at",
      "seerr.days_since_last_requested",
      "seerr.requested_by_user_ids",
      "seerr.requester_has_watched",
      "subtitle.languages",
      "subtitle.has_forced",
      "subtitle.track_count",
      "tmdb.days_since_release",
      "tmdb.in_collection",
      "tmdb.collection_name",
      "tmdb.genres",
      "tmdb.original_language",
      "tmdb.origin_country",
      "tmdb.popularity",
      "tmdb.release_date",
      "tmdb.runtime_minutes",
      "tmdb.vote_average",
      "tmdb.vote_count",
      "video.codec_family",
      "video.bitrate_kbps",
      "video.bit_depth",
      "video.color_primaries",
      "video.color_space",
      "video.color_transfer",
      "video.dolby_vision",
      "video.hdr",
      "video.height",
      "video.resolution",
      "video.width",
      "watch.days_since_last_watched",
      "watch.last_viewed_at",
      "watch.never_watched",
      "watch.view_count",
      ...PLAYBACK_FIELD_VALUES,
      "movie.version_count",
      "rottentomatoes.popcorn_meter",
      "rottentomatoes.popcorn_vote_count",
      "rottentomatoes.tomato_meter",
      "rottentomatoes.tomato_vote_count",
      "trakt.rating",
      "trakt.vote_count",
    ]),
    series: new Set<string>([
      "anilist.favourites",
      "anilist.popularity",
      "anilist.score",
      "arr.monitored",
      "arr.tags",
      "arr.days_since_file_added",
      "audio.channels",
      "audio.codec_family",
      "disk.free_bytes",
      "disk.free_percent",
      "imdb.rating",
      "imdb.vote_count",
      "letterboxd.score",
      "letterboxd.vote_count",
      "metacritic.metascore",
      "metacritic.user_score",
      "metacritic.user_vote_count",
      "metacritic.vote_count",
      "library.id",
      "media.days_since_added",
      "media.file_name",
      "media.path",
      "media.size",
      "media.year",
      "media_server.collections",
      "seerr.requested",
      "seerr.last_requested_at",
      "seerr.days_since_last_requested",
      "seerr.requested_by_user_ids",
      "seerr.requester_has_watched",
      "rottentomatoes.popcorn_meter",
      "rottentomatoes.popcorn_vote_count",
      "rottentomatoes.tomato_meter",
      "rottentomatoes.tomato_vote_count",
      "series.status",
      "series.library_season_count",
      "series.tmdb_season_count",
      "sonarr.latest_season_has_unaired_episodes",
      "sonarr.latest_season_has_finale",
      "subtitle.languages",
      "tmdb.days_since_first_air_date",
      "tmdb.days_since_last_air_date",
      "tmdb.first_air_date",
      "tmdb.last_air_date",
      "tmdb.genres",
      "tmdb.original_language",
      "tmdb.origin_country",
      "tmdb.popularity",
      "tmdb.vote_average",
      "tmdb.vote_count",
      "video.codec_family",
      "video.dolby_vision",
      "video.hdr",
      "video.height",
      "video.width",
      "watch.days_since_last_watched",
      "watch.last_viewed_at",
      "watch.never_watched",
      "watch.view_count",
      ...PLAYBACK_FIELD_VALUES,
      "trakt.rating",
      "trakt.vote_count",
    ]),
    season: new Set<string>([
      "anilist.favourites",
      "anilist.popularity",
      "anilist.score",
      "arr.monitored",
      "arr.tags",
      "arr.days_since_file_added",
      "audio.channels",
      "audio.codec_family",
      "audio.languages",
      "disk.free_bytes",
      "disk.free_percent",
      "imdb.rating",
      "imdb.vote_count",
      "letterboxd.score",
      "letterboxd.vote_count",
      "metacritic.metascore",
      "metacritic.user_score",
      "metacritic.user_vote_count",
      "metacritic.vote_count",
      "library.id",
      "media.days_since_added",
      "media.file_name",
      "media.path",
      "media.size",
      "media.year",
      "media_server.collections",
      "season.air_date",
      "season.days_since_air_date",
      "season.episode_count",
      "season.fully_watched",
      "season.is_latest_season",
      "season.season_number",
      "season.seasons_from_latest",
      "season.watched_percent",
      "seerr.requested",
      "seerr.last_requested_at",
      "seerr.days_since_last_requested",
      "seerr.requested_by_user_ids",
      "seerr.requester_has_watched",
      "rottentomatoes.popcorn_meter",
      "rottentomatoes.popcorn_vote_count",
      "rottentomatoes.tomato_meter",
      "rottentomatoes.tomato_vote_count",
      "series.status",
      "series.library_season_count",
      "series.tmdb_season_count",
      "subtitle.languages",
      "tmdb.days_since_first_air_date",
      "tmdb.days_since_last_air_date",
      "tmdb.first_air_date",
      "tmdb.last_air_date",
      "tmdb.genres",
      "tmdb.original_language",
      "tmdb.origin_country",
      "tmdb.popularity",
      "tmdb.vote_average",
      "tmdb.vote_count",
      "video.codec_family",
      "video.dolby_vision",
      "video.hdr",
      "video.height",
      "video.width",
      "watch.days_since_last_watched",
      "watch.last_viewed_at",
      "watch.never_watched",
      "watch.view_count",
      ...PLAYBACK_FIELD_VALUES,
      "trakt.rating",
      "trakt.vote_count",
    ]),
    episode: new Set<string>([
      "anilist.favourites",
      "anilist.popularity",
      "anilist.score",
      "arr.monitored",
      "arr.tags",
      "arr.days_since_file_added",
      "disk.free_bytes",
      "disk.free_percent",
      "episode.air_date",
      "episode.days_since_air_date",
      "episode.number",
      "episode.season_number",
      "imdb.rating",
      "imdb.vote_count",
      "letterboxd.score",
      "letterboxd.vote_count",
      "metacritic.metascore",
      "metacritic.user_score",
      "metacritic.user_vote_count",
      "metacritic.vote_count",
      "library.id",
      "media.days_since_added",
      "media.file_name",
      "media.path",
      "media.size",
      "media.year",
      "media_server.collections",
      "season.air_date",
      "season.days_since_air_date",
      "season.episode_count",
      "season.fully_watched",
      "season.is_latest_season",
      "season.season_number",
      "season.seasons_from_latest",
      "season.watched_percent",
      "seerr.requested",
      "seerr.last_requested_at",
      "seerr.days_since_last_requested",
      "seerr.requested_by_user_ids",
      "seerr.requester_has_watched",
      "rottentomatoes.popcorn_meter",
      "rottentomatoes.popcorn_vote_count",
      "rottentomatoes.tomato_meter",
      "rottentomatoes.tomato_vote_count",
      "series.status",
      "series.library_season_count",
      "series.tmdb_season_count",
      "tmdb.days_since_first_air_date",
      "tmdb.days_since_last_air_date",
      "tmdb.first_air_date",
      "tmdb.last_air_date",
      "tmdb.genres",
      "tmdb.original_language",
      "tmdb.origin_country",
      "tmdb.popularity",
      "tmdb.vote_average",
      "tmdb.vote_count",
      "watch.days_since_last_watched",
      "watch.last_viewed_at",
      "watch.never_watched",
      "watch.view_count",
      ...PLAYBACK_FIELD_VALUES,
      "trakt.rating",
      "trakt.vote_count",
    ]),
  };

  const scopedFields = $derived.by(() =>
    fields.filter((field) => SCOPE_FIELD_VALUES[targetScope].has(field.value)),
  );

  // organize fields into common and categorized groups for the UI
  const COMMON_FIELD_VALUES = new Set<string>([
    "library.id",
    "media.path",
    "media.size",
    "media.year",
    "watch.never_watched",
    "watch.view_count",
    "watch.days_since_last_watched",
    "watch.last_viewed_at",
    "series.status",
    "tmdb.in_collection",
    "tmdb.original_language",
    "tmdb.vote_average",
    "imdb.rating",
    "rottentomatoes.tomato_meter",
    "rottentomatoes.popcorn_meter",
    "metacritic.metascore",
    "metacritic.user_score",
    "trakt.rating",
    "letterboxd.score",
    "anilist.score",
    "disk.free_percent",
  ]);

  const fieldLabelComparator = (a: FieldConfig, b: FieldConfig) =>
    a.label.localeCompare(b.label, undefined, { sensitivity: "base" });

  const categoryLabel = (fieldValue: string): string => {
    const [prefix] = fieldValue.split(".");
    switch (prefix) {
      case "library":
        return "Library";
      case "media":
        return "Media";
      case "media_server":
        return "Media Server";
      case "watch":
        return "Watch";
      case "playback":
        return "Playback History";
      case "tmdb":
        return "TMDB";
      case "imdb":
        return "IMDb";
      case "rottentomatoes":
        return "Rotten Tomatoes";
      case "metacritic":
        return "Metacritic";
      case "trakt":
        return "Trakt";
      case "letterboxd":
        return "Letterboxd";
      case "anilist":
        return "AniList";
      case "season":
        return "Season";
      case "episode":
        return "Episode";
      case "series":
        return "Series";
      case "movie":
        return "Movie";
      case "video":
        return "Video";
      case "audio":
        return "Audio";
      case "subtitle":
        return "Subtitle";
      case "arr":
        return "Arr";
      case "sonarr":
        return "Sonarr";
      case "seerr":
        return "Seerr";
      case "disk":
        return "Disk";
      default:
        return "Other";
    }
  };

  const groupedFields = $derived.by(() => {
    const groups: FieldGroup[] = [];

    const commonItems = scopedFields
      .filter((field) => COMMON_FIELD_VALUES.has(field.value))
      .sort(fieldLabelComparator);
    if (commonItems.length > 0) {
      groups.push({ key: "common", label: "Common", items: commonItems });
    }

    const byCategory = new Map<string, FieldConfig[]>();
    for (const field of scopedFields) {
      if (COMMON_FIELD_VALUES.has(field.value)) continue;
      const label = categoryLabel(field.value);
      const current = byCategory.get(label) ?? [];
      current.push(field);
      byCategory.set(label, current);
    }

    for (const [label, items] of [...byCategory.entries()].sort((a, b) =>
      a[0].localeCompare(b[0], undefined, { sensitivity: "base" }),
    )) {
      groups.push({
        key: label.toLowerCase(),
        label,
        items: items.sort(fieldLabelComparator),
      });
    }

    return groups;
  });

  const TMDB_SERIES_STATUSES = [
    "Returning Series",
    "Ended",
    "Canceled",
    "In Production",
    "Planned",
    "Pilot",
  ];

  // helpers
  const fieldConfig = (fieldValue: string) =>
    fields.find((f) => f.value === fieldValue) ?? fields[0];

  const operatorOptions = (fieldValue: string) =>
    fieldConfig(fieldValue).operators.map((value) => ({
      value,
      label: operatorLabelMap[value],
    }));

  const fieldLabel = (value: string) =>
    fields.find((f) => f.value === value)?.label ?? value;
  const ruleNodeLabel = (ruleNode: RuleNode) =>
    ruleNode.type === "group"
      ? `${ruleNode.op.toUpperCase()} group`
      : `${fieldLabel(ruleNode.field)} condition`;
  const isFieldCompatibleForScope = (fieldValue: string) =>
    SCOPE_FIELD_VALUES[targetScope].has(fieldValue);
  const operatorLabel = (value: RuleConditionOperator) =>
    operatorLabelMap[value] ?? value;
  const isNumericInput = (c: RuleCondition) =>
    fieldConfig(c.field).kind === "number" && !listOperators.has(c.operator);
  const isTemporalInput = (c: RuleCondition) =>
    fieldConfig(c.field).kind === "temporal" &&
    !valuelessOperators.has(c.operator);
  const valuePlaceholder = (c: RuleCondition) => {
    if (c.operator === "matches_any_regex") return "regex patterns…";
    if (c.field === "seerr.requested_by_user_ids")
      return "Seerr user IDs (comma-separated)...";
    if (c.field === "playback.usernames")
      return "Playback usernames (comma-separated)...";
    if (c.field === "tmdb.collection_name")
      return "Collection names (comma-separated)...";
    if (c.field === "tmdb.genres") return "Genres (comma-separated)...";
    if (c.field === "tmdb.original_language")
      return "Language codes or names (for example: eng, English)...";
    if (c.field === "tmdb.origin_country")
      return "Country codes (for example: US, GB)...";
    if (c.field === "media_server.collections")
      return "Media-server collections (comma-separated)...";
    if (listOperators.has(c.operator)) return "comma-separated…";
    return "value…";
  };

  const valueText = (c: RuleCondition) => {
    const v = c.value;
    if (Array.isArray(v)) return v.join(", ");
    return v === null || v === undefined ? "" : String(v);
  };

  const normalizeValueList = (value: RuleCondition["value"]): string[] =>
    (Array.isArray(value) ? value : value == null ? [] : [value])
      .map((item) => String(item).trim())
      .filter(Boolean);

  const countGroups = (n: RuleNode): number =>
    n.type !== "group"
      ? 0
      : 1 + n.children.reduce((s, c) => s + countGroups(c), 0);

  const canAddGroup = (root: RuleNode) => countGroups(root) < MAX_TOTAL_GROUPS;
  const canNestGroup = (currentDepth: number) => currentDepth < MAX_GROUP_DEPTH;
  const addGroupDisabledTitle = (currentDepth: number) => {
    if (!canAddGroup(rootNode)) return `Max ${MAX_TOTAL_GROUPS} groups`;
    if (!canNestGroup(currentDepth))
      return `Max nesting depth of ${MAX_GROUP_DEPTH}`;
    return undefined;
  };

  const canAcceptActiveNode = (): boolean =>
    node.type === "group" &&
    canMoveRuleNodeToGroup(dragState.activeNode, node, depth, MAX_GROUP_DEPTH);

  const dndOptions = $derived.by(
    (): DndOptions<RuleDndItem> => ({
      items: dndItems,
      type: RULE_DND_TYPE,
      flipDurationMs: 150,
      dropFromOthersDisabled:
        dragState.activeNode !== null && !canAcceptActiveNode(),
      dropTargetClasses: ["rule-drop-target"],
      delayTouchStart: 120,
      useCursorForDetection: true,
    }),
  );

  const handleDndConsider = (event: CustomEvent<DndEvent<RuleDndItem>>) => {
    if (node.type !== "group") return;

    if (event.detail.info.trigger === TRIGGERS.DRAG_STARTED) {
      dragState.activeNode =
        dragState.nodesById.get(event.detail.info.id) ?? null;
      dragState.activeSourceGroup = node;
    }
    dndItems = event.detail.items;
  };

  const scheduleDragStateClear = () => {
    if (dragState.clearScheduled) return;
    dragState.clearScheduled = true;
    queueMicrotask(() => {
      dragState.activeNode = null;
      dragState.activeSourceGroup = null;
      dragState.clearScheduled = false;
    });
  };

  const handleDndFinalize = (event: CustomEvent<DndEvent<RuleDndItem>>) => {
    if (node.type !== "group") return;
    if (
      dragState.activeSourceGroup !== node &&
      dragState.activeNode !== null &&
      !canAcceptActiveNode()
    ) {
      scheduleDragStateClear();
      return;
    }

    dndItems = event.detail.items;
    node.children = dndItems
      .filter((item) => !isShadowItem(item))
      .map((item) => item.node);
    onChange();
    scheduleDragStateClear();
  };

  $effect(() => {
    if (node.type !== "group" || dragState.activeNode !== null) return;
    const children = node.children;
    dndItems = untrack(() => toDndItems(children));
  });

  // mutations
  const ensureValidOperator = (c: RuleCondition) => {
    const config = fieldConfig(c.field);
    if (!config.operators.includes(c.operator))
      c.operator = config.defaultOperator;
  };

  const applyConditionValue = (c: RuleCondition, raw: string) => {
    if (valuelessOperators.has(c.operator)) {
      delete c.value;
      return;
    }
    if (listOperators.has(c.operator)) {
      c.value = raw
        .split(",")
        .map((v) => v.trim())
        .filter(Boolean);
      return;
    }
    if (fieldConfig(c.field).kind === "number") {
      if (raw === "") {
        c.value = null;
        return;
      }
      const parsed = Number(raw);
      c.value = Number.isFinite(parsed) ? parsed : null;
      return;
    }
    c.value = raw;
  };

  const setConditionValue = (c: RuleCondition, raw: string) => {
    applyConditionValue(c, raw);
    onChange();
  };

  const setConditionField = (c: RuleCondition, fieldValue: string) => {
    const raw = valueText(c);
    c.field = fieldValue;
    ensureValidOperator(c);
    // don't carry numeric defaults into path conditions
    if (fieldValue === "media.path") {
      if (valuelessOperators.has(c.operator)) delete c.value;
      else if (listOperators.has(c.operator)) c.value = [];
      else c.value = "";
    } else {
      applyConditionValue(c, raw);
    }
    onChange();
  };

  const selectConditionField = (c: RuleCondition, fieldValue: string) => {
    setConditionField(c, fieldValue);
    fieldPickerOpen = false;
    fieldQuery = "";
  };

  const setConditionOperator = (
    c: RuleCondition,
    op: RuleConditionOperator,
  ) => {
    const raw = valueText(c);
    c.operator = op;
    ensureValidOperator(c);
    applyConditionValue(c, raw);
    onChange();
  };

  const addCondition = (group: RuleGroup) => {
    group.children = [
      ...group.children,
      {
        type: "condition",
        field: "media.size",
        operator: "greater_than",
        value: null,
      },
    ];
    onChange();
  };

  const addGroup = (group: RuleGroup) => {
    group.children = [
      ...group.children,
      { type: "group", op: "and", children: [] },
    ];
    onChange();
  };

  const removeChild = (group: RuleGroup, child: RuleNode) => {
    group.children = group.children.filter((item) => item !== child);
    onChange();
  };

  const addPathPattern = (c: RuleCondition, pattern: string) => {
    const cleaned = pattern.trim();
    if (!cleaned) return;
    if (listOperators.has(c.operator)) {
      const existing = normalizeValueList(c.value);
      if (!existing.includes(cleaned)) {
        c.value = [...existing, cleaned];
        onChange();
      }
      return;
    }
    c.value = cleaned;
    onChange();
  };

  const applySeerrUserIds = (c: RuleCondition, ids: string[]) => {
    if (!listOperators.has(c.operator)) return;
    c.value = ids;
    onChange();
  };

  const applyPlaybackUsernames = (c: RuleCondition, usernames: string[]) => {
    if (!listOperators.has(c.operator)) return;
    c.value = usernames;
    onChange();
  };

  const applyMovieCollections = (c: RuleCondition, names: string[]) => {
    const cleaned = [
      ...new Set(names.map((name) => name.trim()).filter(Boolean)),
    ];
    if (listOperators.has(c.operator)) {
      c.value = cleaned;
    } else {
      c.value = cleaned[0] ?? "";
    }
    onChange();
  };

  const applyGenres = (c: RuleCondition, names: string[]) => {
    const cleaned = [
      ...new Set(names.map((name) => name.trim()).filter(Boolean)),
    ];
    c.value = cleaned;
    onChange();
  };

  const applyMediaServerCollections = (c: RuleCondition, names: string[]) => {
    const cleaned = [
      ...new Set(names.map((name) => name.trim()).filter(Boolean)),
    ];
    c.value = cleaned;
    onChange();
  };

  const applyMetadataValues = (c: RuleCondition, values: string[]) => {
    const cleaned = [
      ...new Set(values.map((value) => value.trim()).filter(Boolean)),
    ];
    c.value = listOperators.has(c.operator) ? cleaned : (cleaned[0] ?? "");
    onChange();
  };

  // styling
  const groupBg = (d: number) =>
    [
      "bg-card",
      "bg-cyan-50/80 dark:bg-cyan-950/30",
      "bg-emerald-50/80 dark:bg-emerald-950/30",
      "bg-amber-50/75 dark:bg-amber-950/28",
      "bg-violet-50/75 dark:bg-violet-950/28",
    ][Math.min(d, 4)];

  const conditionBg = (d: number) =>
    [
      "bg-background",
      "bg-cyan-50/45 dark:bg-cyan-950/20",
      "bg-emerald-50/45 dark:bg-emerald-950/20",
      "bg-amber-50/45 dark:bg-amber-950/18",
      "bg-violet-50/45 dark:bg-violet-950/18",
    ][Math.min(d, 4)];
</script>

{#if node.type === "group"}
  <div
    class:rule-group-root={depth === 0}
    class:rule-group-nested={depth > 0}
    style={`--rule-depth: ${depth}`}
  >
    <div
      class={`rule-group-card rounded-lg border border-border/70 ${groupBg(depth)} overflow-hidden`}
    >
      <!-- group header -->
      <div
        class="rule-group-header flex items-start md:items-center gap-2 px-2 py-2 md:px-4 md:py-2.5 border-b border-border/60"
      >
        {#if onRemove}
          <button
            type="button"
            use:dragHandle
            aria-label={`Move ${ruleNodeLabel(node)}`}
            title="Drag to move this group"
            class="rule-drag-handle flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <GripVertical class="size-4" />
          </button>
        {/if}

        <!-- AND / OR toggle -->
        <div
          class="flex items-center rounded-md border border-border overflow-hidden shrink-0"
        >
          {#each ["and", "or"] as const as op}
            <button
              class={`px-2.5 py-1 text-xs font-bold transition-colors cursor-pointer ${
                node.op === op
                  ? "bg-primary text-primary-foreground"
                  : "bg-transparent text-muted-foreground hover:text-foreground"
              }`}
              onclick={() => {
                node.op = op;
                onChange();
              }}
            >
              {op.toUpperCase()}
            </button>
          {/each}
        </div>

        <span class="min-w-0 text-xs text-muted-foreground grow">
          <span class="md:hidden">
            {node.op === "and" ? "All match" : "Any match"}
          </span>
          <span class="hidden md:inline">
            {node.op === "and"
              ? "All conditions must match"
              : "Any condition can match"}
          </span>
        </span>

        {#if onRemove}
          <Button
            size="icon-sm"
            class="cursor-pointer bg-destructive/80 hover:bg-destructive/90 text-destructive-foreground shrink-0"
            onclick={onRemove}
          >
            <Trash2 class="size-3.5" />
          </Button>
        {/if}
      </div>

      <!-- children -->
      <div
        use:dragHandleZone={dndOptions}
        onconsider={handleDndConsider}
        onfinalize={handleDndFinalize}
        aria-label={`Items in ${ruleNodeLabel(node)}`}
        class:rule-drop-disabled={dragState.activeNode !== null &&
          !canAcceptActiveNode()}
        class="rule-group-children p-2 md:p-3 space-y-2"
      >
        {#each dndItems as item (item.id)}
          <div
            class:rule-dnd-shadow={isShadowItem(item)}
            class="rule-dnd-item"
            aria-label={`Move ${ruleNodeLabel(item.node)}`}
          >
            {#if isShadowItem(item)}
              <div
                class="rule-drop-placeholder rounded-md border-2 border-dashed border-primary/60 bg-primary/10 px-3 py-4 text-center text-xs font-medium text-primary"
              >
                Drop here
              </div>
            {:else}
              <Self
                node={item.node}
                {rootNode}
                depth={depth + 1}
                {targetScope}
                {pathPickerMediaType}
                {pathPickerLibraryIds}
                {onChange}
                onRemove={() => removeChild(node, item.node)}
              />
            {/if}
          </div>
        {/each}
      </div>

      <!-- footer actions -->
      <div
        class="rule-group-footer flex flex-wrap items-center gap-2 px-2 pb-2 md:px-3 md:pb-3"
      >
        <Button
          size="sm"
          variant="secondary"
          class="h-8 text-xs gap-1.5 cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground"
          onclick={() => addCondition(node)}
        >
          <Plus class="size-3.5" />
          Add condition
        </Button>

        <Button
          size="sm"
          variant="secondary"
          class="h-8 text-xs gap-1.5 cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground"
          onclick={() => addGroup(node)}
          disabled={!canAddGroup(rootNode) || !canNestGroup(depth)}
          title={addGroupDisabledTitle(depth)}
        >
          <Plus class="size-3.5" />
          Add group
        </Button>

        {#if !canAddGroup(rootNode)}
          <span class="text-xs text-muted-foreground"
            >Max {MAX_TOTAL_GROUPS} groups reached</span
          >
        {/if}

        {#if !canNestGroup(depth)}
          <span class="text-xs text-muted-foreground"
            >Max nesting depth reached</span
          >
        {/if}
      </div>
    </div>
  </div>
{:else}
  <!-- condition row -->
  <div
    class={`rule-condition relative rounded-md border border-border/70 py-2 pr-2 pl-10 md:py-2.5 md:pr-3 md:pl-11 ${conditionBg(depth)}`}
    style={`--rule-depth: ${depth}`}
  >
    {#if onRemove}
      <button
        type="button"
        use:dragHandle
        aria-label={`Move ${ruleNodeLabel(node)}`}
        title="Drag to move this condition"
        class="rule-drag-handle absolute top-2 left-1.5 flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:top-2.5 md:left-2"
      >
        <GripVertical class="size-4" />
      </button>
    {/if}

    <!--
      Layout strategy (attempt to keep things nice):
      - stack vertically on mobile (flex-col)
      - switch to a wrapping row on md+ (flex-row flex-wrap)
      - value + browse share a row via their own inner flex so browse never 
      gets squeezed out at awkward breakpoints
    -->
    <div
      class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 md:flex md:flex-row md:flex-wrap md:items-center"
    >
      <!-- field selector -->
      <Popover.Root
        open={fieldPickerOpen}
        onOpenChange={(open) => {
          fieldPickerOpen = open;
          if (!open) fieldQuery = "";
        }}
      >
        <Popover.Trigger
          class="col-start-1 row-start-1 w-full min-w-0 md:col-auto md:row-auto md:w-auto md:flex-1 md:min-w-36"
        >
          <Button
            type="button"
            variant="outline"
            aria-label="Select rule field"
            aria-expanded={fieldPickerOpen}
            class="h-8 w-full min-w-0 justify-between bg-background px-3 text-sm font-normal text-foreground cursor-pointer"
          >
            <span class="truncate">{fieldLabel(node.field)}</span>
            <ChevronsUpDown class="size-4 shrink-0 opacity-60" />
          </Button>
        </Popover.Trigger>
        <Popover.Content
          align="start"
          class="w-(--bits-popover-anchor-width) min-w-72 p-0 gap-0"
        >
          <Command.Root class="rounded-md p-0">
            <Command.Input
              bind:value={fieldQuery}
              placeholder="Search rule fields..."
            />
            <Command.List class="max-h-80">
              <Command.Empty>No matching rule fields.</Command.Empty>
              {#each groupedFields as group (group.key)}
                <Command.Group heading={group.label}>
                  {#each group.items as field (field.value)}
                    <Command.Item
                      value={field.value}
                      keywords={[field.label, group.label, field.value]}
                      onSelect={() => selectConditionField(node, field.value)}
                    >
                      {field.label}
                    </Command.Item>
                  {/each}
                </Command.Group>
              {/each}
            </Command.List>
          </Command.Root>
        </Popover.Content>
      </Popover.Root>

      <!-- operator selector -->
      <Select.Root
        type="single"
        value={node.operator}
        onValueChange={(value) =>
          setConditionOperator(node, value as RuleConditionOperator)}
      >
        <Select.Trigger
          class="col-span-2 row-start-2 h-8 w-full min-w-0 md:col-auto md:row-auto md:w-auto md:flex-1 md:min-w-32 text-sm text-foreground cursor-pointer bg-background"
        >
          {operatorLabel(node.operator)}
        </Select.Trigger>
        <Select.Content>
          {#each operatorOptions(node.field) as op}
            <Select.Item value={op.value} label={op.label}
              >{op.label}</Select.Item
            >
          {/each}
        </Select.Content>
      </Select.Root>

      <!-- value + optional browse button - always on their own flex row so browse can't get pushed off 
        screen by the selects above -->
      {#if !valuelessOperators.has(node.operator)}
        <div
          class="col-span-2 row-start-3 flex flex-wrap items-center gap-2 w-full min-w-0 md:col-auto md:row-auto md:flex-1 md:min-w-[18rem]"
        >
          {#if node.field === "series.status" && !listOperators.has(node.operator)}
            <Select.Root
              type="single"
              value={typeof node.value === "string" ? node.value : ""}
              onValueChange={(v) => setConditionValue(node, v)}
            >
              <Select.Trigger
                class="h-8 flex-1 text-sm text-foreground cursor-pointer bg-background"
              >
                {typeof node.value === "string" && node.value
                  ? node.value
                  : "Select status…"}
              </Select.Trigger>
              <Select.Content>
                {#each TMDB_SERIES_STATUSES as status}
                  <Select.Item value={status} label={status}
                    >{status}</Select.Item
                  >
                {/each}
              </Select.Content>
            </Select.Root>
          {:else}
            <Input
              class="h-8 flex-1 min-w-0 text-sm text-foreground placeholder:text-muted-foreground bg-background"
              type={isNumericInput(node)
                ? "number"
                : isTemporalInput(node)
                  ? "date"
                  : "text"}
              placeholder={valuePlaceholder(node)}
              value={valueText(node)}
              oninput={(e) => setConditionValue(node, e.currentTarget.value)}
            />
            {#if node.field === "seerr.requested_by_user_ids" && listOperators.has(node.operator)}
              <Button
                size="sm"
                variant="secondary"
                class="h-8 text-xs gap-1.5 cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground shrink-0"
                onclick={() => (seerrPickerOpen = true)}
              >
                <Users class="size-3.5" />
                <span class="hidden md:inline">Pick Users</span>
              </Button>
            {/if}
            {#if node.field === "playback.usernames" && listOperators.has(node.operator)}
              <Button
                size="sm"
                variant="secondary"
                class="h-8 text-xs gap-1.5 cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground shrink-0"
                onclick={() => (playbackUserPickerOpen = true)}
              >
                <Users class="size-3.5" />
                <span class="hidden md:inline">Pick Users</span>
              </Button>
            {/if}
            {#if node.field === "tmdb.collection_name"}
              <Button
                size="sm"
                variant="secondary"
                class="h-8 text-xs cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground shrink-0"
                onclick={() => (collectionPickerOpen = true)}
              >
                <span class="hidden md:inline">Pick Collections</span>
                <span class="md:hidden">Pick</span>
              </Button>
            {/if}
            {#if node.field === "tmdb.genres" && pathPickerMediaType}
              <Button
                size="sm"
                variant="secondary"
                class="h-8 text-xs cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground shrink-0"
                onclick={() => (genrePickerOpen = true)}
              >
                <span class="hidden md:inline">Pick Genres</span>
                <span class="md:hidden">Pick</span>
              </Button>
            {/if}
            {#if node.field === "tmdb.original_language" && pathPickerMediaType}
              <Button
                size="sm"
                variant="secondary"
                class="h-8 text-xs cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground shrink-0"
                onclick={() => (originalLanguagePickerOpen = true)}
              >
                <span class="hidden md:inline">Pick Languages</span>
                <span class="md:hidden">Pick</span>
              </Button>
            {/if}
            {#if node.field === "tmdb.origin_country" && pathPickerMediaType}
              <Button
                size="sm"
                variant="secondary"
                class="h-8 text-xs cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground shrink-0"
                onclick={() => (originCountryPickerOpen = true)}
              >
                <span class="hidden md:inline">Pick Countries</span>
                <span class="md:hidden">Pick</span>
              </Button>
            {/if}
            {#if node.field === "media_server.collections" && pathPickerMediaType}
              <Button
                size="sm"
                variant="secondary"
                class="h-8 text-xs cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground shrink-0"
                onclick={() => (mediaServerCollectionPickerOpen = true)}
              >
                <span class="hidden md:inline">Pick Collections</span>
                <span class="md:hidden">Pick</span>
              </Button>
            {/if}
            {#if node.field === "media.path" && node.operator === "matches_any_regex" && pathPickerMediaType}
              <Button
                size="sm"
                variant="secondary"
                class="h-8 text-xs gap-1.5 cursor-pointer bg-secondary/75 hover:bg-secondary/90 text-foreground shrink-0"
                onclick={() => (pathPickerOpen = true)}
              >
                <FolderSearch class="size-3.5" />
                <span class="hidden md:inline">Browse</span>
              </Button>
            {/if}
          {/if}
        </div>
      {:else}
        <!-- spacer so the remove button stays right aligned on desktop -->
        <div class="hidden md:flex md:flex-1"></div>
      {/if}

      <!-- remove button -->
      {#if onRemove}
        <Button
          size="icon-sm"
          class="col-start-2 row-start-1 cursor-pointer bg-destructive/80 hover:bg-destructive/90 text-destructive-foreground self-center shrink-0 md:col-auto md:row-auto"
          onclick={onRemove}
        >
          <Trash2 class="size-3.5" />
        </Button>
      {/if}

      {#if !isFieldCompatibleForScope(node.field)}
        <p class="w-full text-xs text-amber-600 dark:text-amber-400">
          This field is not available for the current target scope.
        </p>
      {/if}
    </div>
    {#if node.field === "movie.version_count"}
      <p class="mt-2 text-xs text-amber-700 dark:text-amber-300">
        Version count alone selects every version of a multi-version movie.
        Combine it with quality, codec, size, bitrate, or resolution conditions.
      </p>
    {/if}
  </div>

  {#if node.field === "media.path" && node.operator === "matches_any_regex" && pathPickerMediaType}
    <PathPatternPicker
      bind:open={pathPickerOpen}
      mediaType={pathPickerMediaType}
      libraryIds={pathPickerLibraryIds}
      onSelect={(pattern) => addPathPattern(node, pattern)}
    />
  {/if}

  {#if node.field === "seerr.requested_by_user_ids" && listOperators.has(node.operator)}
    <SeerrUserPicker
      bind:open={seerrPickerOpen}
      initialSelectedIds={normalizeValueList(node.value)}
      onApply={(ids) => applySeerrUserIds(node, ids)}
    />
  {/if}

  {#if node.field === "playback.usernames" && listOperators.has(node.operator)}
    <PlaybackUserPicker
      bind:open={playbackUserPickerOpen}
      initialSelectedUsernames={normalizeValueList(node.value)}
      onApply={(usernames) => applyPlaybackUsernames(node, usernames)}
    />
  {/if}

  {#if node.field === "tmdb.collection_name"}
    <MovieCollectionPicker
      bind:open={collectionPickerOpen}
      initialSelectedNames={normalizeValueList(node.value)}
      allowMultiple={listOperators.has(node.operator)}
      onApply={(names) => applyMovieCollections(node, names)}
    />
  {/if}

  {#if node.field === "tmdb.genres" && pathPickerMediaType}
    <GenrePicker
      bind:open={genrePickerOpen}
      mediaType={pathPickerMediaType}
      initialSelectedNames={normalizeValueList(node.value)}
      onApply={(names) => applyGenres(node, names)}
    />
  {/if}

  {#if node.field === "tmdb.original_language" && pathPickerMediaType}
    <MetadataValuePicker
      bind:open={originalLanguagePickerOpen}
      mediaType={pathPickerMediaType}
      endpoint="/api/rules/original-languages"
      title="Select Original Languages"
      description="Search original languages found in local TMDB metadata. You can still type a language manually."
      searchPlaceholder="Search languages or codes..."
      emptyLabel="No original languages found."
      initialSelectedValues={normalizeValueList(node.value)}
      onApply={(values) => applyMetadataValues(node, values)}
    />
  {/if}

  {#if node.field === "tmdb.origin_country" && pathPickerMediaType}
    <MetadataValuePicker
      bind:open={originCountryPickerOpen}
      mediaType={pathPickerMediaType}
      endpoint="/api/rules/origin-countries"
      title="Select Origin Countries"
      description="Search origin-country codes found in local TMDB metadata. You can still type a code manually."
      searchPlaceholder="Search country codes..."
      emptyLabel="No origin countries found."
      initialSelectedValues={normalizeValueList(node.value)}
      onApply={(values) => applyMetadataValues(node, values)}
    />
  {/if}

  {#if node.field === "media_server.collections" && pathPickerMediaType}
    <MediaServerCollectionPicker
      bind:open={mediaServerCollectionPickerOpen}
      mediaType={pathPickerMediaType}
      initialSelectedNames={normalizeValueList(node.value)}
      onApply={(names) => applyMediaServerCollections(node, names)}
    />
  {/if}
{/if}

<style>
  .rule-drag-handle {
    touch-action: none;
    user-select: none;
  }

  .rule-group-children {
    min-height: 2rem;
    transition:
      outline-color 120ms ease,
      background-color 120ms ease,
      opacity 120ms ease;
  }

  .rule-group-children:empty::after {
    display: block;
    padding: 0.75rem;
    border: 1px dashed color-mix(in oklab, var(--border) 80%, transparent);
    border-radius: 0.375rem;
    color: var(--muted-foreground);
    font-size: 0.75rem;
    text-align: center;
    content: "Drop rule nodes here";
  }

  .rule-drop-disabled {
    opacity: 0.55;
  }

  :global(.rule-drop-target) {
    outline: 2px solid color-mix(in oklab, var(--primary) 65%, transparent);
    outline-offset: -2px;
    background-color: color-mix(in oklab, var(--primary) 8%, transparent);
  }

  .rule-dnd-shadow {
    opacity: 0.9;
  }

  .rule-group-root {
    padding-left: 0;
  }

  .rule-group-nested {
    margin-left: 0.125rem;
    padding-left: 0.25rem;
    border-left: 2px solid color-mix(in oklab, var(--border) 75%, transparent);
  }

  .rule-group-nested .rule-group-children {
    padding: 0.375rem;
  }

  .rule-group-nested .rule-group-footer {
    padding-inline: 0.375rem;
    padding-bottom: 0.375rem;
  }

  .rule-condition {
    margin-left: 0;
  }

  @media (min-width: 768px) {
    .rule-group-nested {
      margin-left: 0;
      padding-left: calc(var(--rule-depth) * 12px);
      border-left: 0;
    }

    .rule-group-nested .rule-group-children {
      padding: 0.75rem;
    }

    .rule-group-nested .rule-group-footer {
      padding-inline: 0.75rem;
      padding-bottom: 0.75rem;
    }

    .rule-condition {
      margin-left: calc(var(--rule-depth) * 12px);
    }
  }
</style>
