<script lang="ts">
  import { onMount } from "svelte";
  import type { Component } from "svelte";
  import { get_api, put_api } from "$lib/api";
  import { toast } from "svelte-sonner";
  import { Button } from "$lib/components/ui/button/index.js";
  import * as Command from "$lib/components/ui/command/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Popover from "$lib/components/ui/popover/index.js";
  import * as Select from "$lib/components/ui/select/index.js";
  import Spinner from "$lib/components/ui/spinner/spinner.svelte";
  import ChevronsUpDown from "@lucide/svelte/icons/chevrons-up-down";
  import Save from "@lucide/svelte/icons/save";
  import Plus from "@lucide/svelte/icons/plus";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
  import FavoritesSettings from "$lib/components/settings/general/favorites-settings.svelte";
  import {
    type GeneralSettings,
    type RequesterWatchUserMapping,
    type SeerrUserLookup,
    type WatchUserLookup,
  } from "$lib/types/shared";
  import { DEFAULT_NEW_USER_ALLOWED_PAGES } from "$lib/page-access";

  interface Props {
    svgIcon: Component | null;
  }

  let { svgIcon }: Props = $props();

  const DEFAULT_GENERAL_SETTINGS: GeneralSettings = {
    worker_poll_min_seconds: null,
    worker_poll_max_seconds: null,
    path_mappings: [],
    post_action_webhooks: [],
    move_enabled: false,
    move_destination_movies: null,
    move_destination_series: null,
    media_server_fallback_enabled: true,
    default_arr_delete_behavior: "unmonitor",
    add_arr_import_exclusions_on_delete: true,
    auto_delete_enabled: false,
    application_url: null,
    favorites_ignore_enabled: false,
    favorites_protect_all_users: false,
    favorites_usernames: [],
    requester_watch_user_mappings: [],
    default_allowed_pages: [...DEFAULT_NEW_USER_ALLOWED_PAGES],
    leaving_soon_enabled: false,
    leaving_soon_collection_title: "Leaving Soon",
  };

  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let savingSettings = $state(false);
  let refreshingLookups = $state(false);
  let favoritesIgnoreEnabled = $state(false);
  let favoritesProtectAllUsers = $state(false);
  let favoritesUsernamesInput = $state("");
  let requesterWatchUserMappings = $state<RequesterWatchUserMapping[]>([]);
  let baseSettings = $state<GeneralSettings>(DEFAULT_GENERAL_SETTINGS);
  let seerrUsers = $state<SeerrUserLookup[]>([]);
  let watchUsers = $state<WatchUserLookup[]>([]);
  let mappingSearch = $state("");
  let mappingSort = $state<
    "seerr_asc" | "seerr_desc" | "media_asc" | "media_desc" | "service_asc"
  >("seerr_asc");
  let mediaUserPickerRowIndex = $state<number | null>(null);
  let mediaUserQuery = $state("");

  type MappingRow = {
    index: number;
    mapping: RequesterWatchUserMapping;
    seerrDisplay: string;
    mediaKey: string;
    serviceLabel: string;
  };

  const parseFavoritesUsernames = (value: string): string[] => {
    const seen = new Set<string>();
    const usernames: string[] = [];
    for (const raw of value.split(/[\n,]+/)) {
      const normalized = raw.trim().toLowerCase();
      if (!normalized || seen.has(normalized)) continue;
      seen.add(normalized);
      usernames.push(normalized);
    }
    return usernames;
  };

  const cleanMappings = (
    value: RequesterWatchUserMapping[],
  ): RequesterWatchUserMapping[] => {
    const cleaned: RequesterWatchUserMapping[] = [];
    for (const mapping of value) {
      const mediaUserKey = String(mapping.media_user_key ?? "").trim();
      const username = String(mapping.seerr_username ?? "")
        .trim()
        .toLowerCase();
      const userId =
        mapping.seerr_user_id !== null && Number.isFinite(mapping.seerr_user_id)
          ? Number(mapping.seerr_user_id)
          : null;
      const serviceType =
        mapping.service_type && String(mapping.service_type).trim()
          ? String(mapping.service_type).trim().toLowerCase()
          : null;
      if (!mediaUserKey || (userId === null && !username)) continue;
      cleaned.push({
        seerr_user_id: userId,
        seerr_username: username || null,
        media_user_key: mediaUserKey,
        service_type: serviceType,
      });
    }
    return cleaned;
  };

  const addMapping = () => {
    requesterWatchUserMappings = [
      ...requesterWatchUserMappings,
      {
        seerr_user_id: null,
        seerr_username: null,
        media_user_key: "",
        service_type: null,
      },
    ];
  };

  const removeMapping = (index: number) => {
    if (mediaUserPickerRowIndex === index) {
      mediaUserPickerRowIndex = null;
      mediaUserQuery = "";
    } else if (
      mediaUserPickerRowIndex !== null &&
      mediaUserPickerRowIndex > index
    ) {
      mediaUserPickerRowIndex -= 1;
    }
    requesterWatchUserMappings = requesterWatchUserMappings.filter(
      (_, i) => i !== index,
    );
  };

  const seerrLabel = (user: SeerrUserLookup): string => {
    const display = user.display_name || user.username || `User ${user.id}`;
    const username = user.username ? ` (@${user.username})` : "";
    return `${display}${username} [${user.id}]`;
  };

  const seerrSelectValue = (mapping: RequesterWatchUserMapping): string =>
    mapping.seerr_user_id !== null && Number.isFinite(mapping.seerr_user_id)
      ? String(mapping.seerr_user_id)
      : "__manual__";

  const seerrTriggerLabel = (mapping: RequesterWatchUserMapping): string => {
    if (
      mapping.seerr_user_id !== null &&
      Number.isFinite(mapping.seerr_user_id)
    ) {
      const user = seerrUsers.find(
        (entry) => entry.id === mapping.seerr_user_id,
      );
      return user ? seerrLabel(user) : `ID ${mapping.seerr_user_id}`;
    }
    return "Manual username only";
  };

  const normalizeKey = (value: unknown): string =>
    String(value ?? "")
      .trim()
      .toLowerCase();

  const displaySeerrIdentity = (mapping: RequesterWatchUserMapping): string => {
    if (
      mapping.seerr_user_id !== null &&
      Number.isFinite(mapping.seerr_user_id)
    ) {
      const user = seerrUsers.find(
        (entry) => entry.id === mapping.seerr_user_id,
      );
      if (user) return seerrLabel(user);
      return `ID ${mapping.seerr_user_id}`;
    }
    if (mapping.seerr_username) return `@${mapping.seerr_username}`;
    return "Unassigned";
  };

  const serviceScopeLabel = (serviceType: string | null): string =>
    serviceType ? serviceType : "Any service";

  const serviceScopeValue = (mapping: RequesterWatchUserMapping): string =>
    mapping.service_type ? mapping.service_type : "__any__";

  const isMediaUserPickerOpen = (rowIndex: number): boolean =>
    mediaUserPickerRowIndex === rowIndex;

  const setMediaUserPickerOpen = (
    rowIndex: number,
    open: boolean,
    initialValue: string,
  ) => {
    if (open) {
      mediaUserPickerRowIndex = rowIndex;
      mediaUserQuery = initialValue;
      return;
    }
    if (mediaUserPickerRowIndex === rowIndex) {
      mediaUserPickerRowIndex = null;
      mediaUserQuery = "";
    }
  };

  const selectMediaUserKey = (rowIndex: number, key: string) => {
    const nextValue = key.trim();
    if (!nextValue || !requesterWatchUserMappings[rowIndex]) return;
    requesterWatchUserMappings[rowIndex].media_user_key = nextValue;
    mediaUserPickerRowIndex = null;
    mediaUserQuery = "";
  };

  const filteredWatchUsersForQuery = $derived.by(() => {
    const needle = normalizeKey(mediaUserQuery);
    if (!needle) return watchUsers;
    return watchUsers.filter((user) => {
      if (
        normalizeKey(user.user_key).includes(needle) ||
        normalizeKey(user.user_key_normalized).includes(needle)
      ) {
        return true;
      }
      return user.source_services.some((service) =>
        normalizeKey(service).includes(needle),
      );
    });
  });

  const mediaUserQueryMatchesKnownUser = $derived.by(() => {
    const needle = normalizeKey(mediaUserQuery);
    if (!needle) return false;
    return watchUsers.some(
      (user) =>
        normalizeKey(user.user_key) === needle ||
        normalizeKey(user.user_key_normalized) === needle,
    );
  });

  const visibleMappings = $derived.by(() => {
    const rows: MappingRow[] = requesterWatchUserMappings.map(
      (mapping, index) => ({
        index,
        mapping,
        seerrDisplay: displaySeerrIdentity(mapping),
        mediaKey: String(mapping.media_user_key ?? ""),
        serviceLabel: serviceScopeLabel(mapping.service_type),
      }),
    );

    const needle = normalizeKey(mappingSearch);
    let filtered = rows;
    if (needle) {
      filtered = rows.filter((row) => {
        const haystack = [
          row.seerrDisplay,
          row.mapping.seerr_username ?? "",
          row.mapping.seerr_user_id ?? "",
          row.mediaKey,
          row.serviceLabel,
        ]
          .join(" ")
          .toLowerCase();
        return haystack.includes(needle);
      });
    }

    const sortValue = (row: MappingRow): string => {
      if (mappingSort.startsWith("seerr_"))
        return normalizeKey(row.seerrDisplay);
      if (mappingSort.startsWith("media_")) return normalizeKey(row.mediaKey);
      return normalizeKey(row.serviceLabel);
    };
    const sorted = [...filtered].sort((a, b) =>
      sortValue(a).localeCompare(sortValue(b)),
    );
    if (mappingSort.endsWith("_desc")) sorted.reverse();
    return sorted;
  });

  const mappingHealth = $derived.by(() => {
    const normalizedWatchKeys = new Set(
      watchUsers.map((entry) => normalizeKey(entry.user_key_normalized)),
    );
    const cleaned = cleanMappings(requesterWatchUserMappings);
    const mappedIds = new Set<number>();
    const mappedNames = new Set<string>();
    for (const mapping of cleaned) {
      if (mapping.seerr_user_id !== null) mappedIds.add(mapping.seerr_user_id);
      if (mapping.seerr_username)
        mappedNames.add(normalizeKey(mapping.seerr_username));
    }

    let covered = 0;
    let coveredByMapping = 0;
    let coveredByDirectKey = 0;
    const unmappedUsers: SeerrUserLookup[] = [];
    for (const user of seerrUsers) {
      const identities = new Set<string>([
        normalizeKey(user.username),
        normalizeKey(user.display_name),
        normalizeKey(user.id),
      ]);
      identities.delete("");

      const hasExplicitMapping =
        mappedIds.has(user.id) ||
        [...identities].some((identity) => mappedNames.has(identity));
      const hasDirectWatchKey = [...identities].some((identity) =>
        normalizedWatchKeys.has(identity),
      );
      if (hasExplicitMapping || hasDirectWatchKey) {
        covered += 1;
        if (hasExplicitMapping) coveredByMapping += 1;
        else coveredByDirectKey += 1;
      } else {
        unmappedUsers.push(user);
      }
    }

    return {
      totalSeerrUsers: seerrUsers.length,
      covered,
      coveredByMapping,
      coveredByDirectKey,
      unmappedCount: unmappedUsers.length,
      unmappedPreview: unmappedUsers.slice(0, 8),
      mappingRows: cleaned.length,
    };
  });

  const loadLookups = async (refresh = false) => {
    const params = new URLSearchParams();
    if (refresh) params.set("refresh", "true");
    const query = params.toString();
    const [loadedSeerrUsers, loadedWatchUsers] = await Promise.all([
      get_api<SeerrUserLookup[]>(`/api/rules/seerr-users?limit=250`),
      get_api<WatchUserLookup[]>(
        `/api/settings/general/watch-users${query ? `?${query}` : ""}`,
      ),
    ]);
    seerrUsers = loadedSeerrUsers ?? [];
    watchUsers = loadedWatchUsers ?? [];
  };

  const refreshLookups = async () => {
    refreshingLookups = true;
    try {
      // Keep the action responsive by reloading from current snapshots.
      // Forcing a server-side snapshot rebuild can take a long time.
      await loadLookups(false);
      toast.success("User lookup data refreshed.");
    } catch (error) {
      toast.error(
        `Failed to refresh lookup data: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      refreshingLookups = false;
    }
  };

  const saveSettings = async () => {
    if (loadError) {
      toast.error("Cannot save while settings failed to load.");
      return;
    }
    savingSettings = true;
    try {
      await put_api("/api/settings/general", {
        ...baseSettings,
        favorites_ignore_enabled: favoritesIgnoreEnabled,
        favorites_protect_all_users: favoritesProtectAllUsers,
        favorites_usernames: parseFavoritesUsernames(favoritesUsernamesInput),
        requester_watch_user_mappings: cleanMappings(
          requesterWatchUserMappings,
        ),
      });
      baseSettings = {
        ...baseSettings,
        favorites_ignore_enabled: favoritesIgnoreEnabled,
        favorites_protect_all_users: favoritesProtectAllUsers,
        favorites_usernames: parseFavoritesUsernames(favoritesUsernamesInput),
        requester_watch_user_mappings: cleanMappings(
          requesterWatchUserMappings,
        ),
      };
      toast.success("User signals settings saved");
    } catch (error) {
      toast.error(
        `Failed to save user signals settings: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      savingSettings = false;
    }
  };

  onMount(async () => {
    try {
      loadError = null;
      const settings = await get_api<GeneralSettings>("/api/settings/general");
      baseSettings = { ...DEFAULT_GENERAL_SETTINGS, ...(settings ?? {}) };
      favoritesIgnoreEnabled = baseSettings.favorites_ignore_enabled ?? false;
      favoritesProtectAllUsers =
        baseSettings.favorites_protect_all_users ?? false;
      favoritesUsernamesInput = (baseSettings.favorites_usernames ?? []).join(
        ", ",
      );
      requesterWatchUserMappings =
        baseSettings.requester_watch_user_mappings ?? [];
      await loadLookups(false);
    } catch (error) {
      loadError =
        error instanceof Error ? error.message : "Failed to load settings";
      toast.error(
        `Failed to load user signals settings: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      loading = false;
    }
  });
</script>

<div class="space-y-6">
  <div>
    <h2 class="flex items-center gap-3 text-xl font-semibold text-foreground">
      {#if svgIcon}
        {@const Icon = svgIcon}
        <Icon class="size-5" aria-hidden="true" />
      {/if}
      User Signals
    </h2>
    <p class="text-sm text-muted-foreground mt-1">
      Manage user identity and watched/favorites mapping behavior.
    </p>
  </div>

  {#if loading}
    <div class="flex justify-center py-8">
      <Spinner class="w-12 h-12 text-primary" />
    </div>
  {:else}
    {#if loadError}
      <div
        class="rounded-md border border-destructive/40 bg-destructive/10 p-3"
      >
        <p class="text-sm text-destructive">
          Failed to load current settings: {loadError}
        </p>
      </div>
    {/if}

    <FavoritesSettings
      bind:favoritesIgnoreEnabled
      bind:favoritesProtectAllUsers
      bind:favoritesUsernamesInput
    />

    <div class="bg-muted/50 border rounded-lg p-4 shadow-sm space-y-4">
      <div
        class="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between"
      >
        <div>
          <h3 class="font-semibold text-foreground">
            Seerr Requester to Watch User Mapping
          </h3>
          <p class="text-sm text-muted-foreground">
            Map one Seerr requester to one or more media-server user keys. You
            can scope each mapping to a specific service or leave it global.
          </p>
        </div>
        <Button
          size="sm"
          type="button"
          class="cursor-pointer gap-2"
          onclick={refreshLookups}
          disabled={refreshingLookups}
        >
          {#if refreshingLookups}
            <Spinner class="size-4" />
          {:else}
            <RotateCcw class="size-4" />
          {/if}
          Reload users
        </Button>
      </div>

      <div
        class="rounded-md border border-border bg-background/50 p-3 space-y-2"
      >
        <div class="flex flex-wrap items-center gap-3 text-xs">
          <span class="rounded-md bg-muted px-2 py-1 text-foreground">
            Seerr users: {mappingHealth.totalSeerrUsers}
          </span>
          <span class="rounded-md bg-muted px-2 py-1 text-foreground">
            Covered: {mappingHealth.covered}
          </span>
          <span class="rounded-md bg-muted px-2 py-1 text-foreground">
            Explicit mappings: {mappingHealth.coveredByMapping}
          </span>
          <span class="rounded-md bg-muted px-2 py-1 text-foreground">
            Direct key matches: {mappingHealth.coveredByDirectKey}
          </span>
          <span
            class="rounded-md px-2 py-1 {mappingHealth.unmappedCount > 0
              ? 'bg-amber-500/20 text-amber-600'
              : 'bg-emerald-500/20 text-emerald-600'}"
          >
            Unmapped: {mappingHealth.unmappedCount}
          </span>
          <span class="rounded-md bg-muted px-2 py-1 text-foreground">
            Mapping rows: {mappingHealth.mappingRows}
          </span>
        </div>
        {#if mappingHealth.unmappedPreview.length > 0}
          <p class="text-xs text-muted-foreground">
            Unmapped preview:
            {mappingHealth.unmappedPreview
              .map(
                (user) =>
                  user.display_name || user.username || `User ${user.id}`,
              )
              .join(", ")}
            {mappingHealth.unmappedCount > mappingHealth.unmappedPreview.length
              ? ` +${mappingHealth.unmappedCount - mappingHealth.unmappedPreview.length} more`
              : ""}
          </p>
        {/if}
      </div>

      <div class="grid gap-2 md:grid-cols-[1fr_11rem]">
        <div>
          <Label class="mb-2 text-xs">Filter mappings</Label>
          <Input
            type="text"
            class="text-sm"
            placeholder="Search Seerr user, media key, service..."
            bind:value={mappingSearch}
          />
        </div>
        <div>
          <Label class="mb-2 text-xs">Sort by</Label>
          <Select.Root
            type="single"
            value={mappingSort}
            onValueChange={(value) =>
              (mappingSort = value as
                | "seerr_asc"
                | "seerr_desc"
                | "media_asc"
                | "media_desc"
                | "service_asc")}
          >
            <Select.Trigger class="w-full cursor-pointer text-foreground">
              {mappingSort === "seerr_asc"
                ? "Seerr user A-Z"
                : mappingSort === "seerr_desc"
                  ? "Seerr user Z-A"
                  : mappingSort === "media_asc"
                    ? "Media key A-Z"
                    : mappingSort === "media_desc"
                      ? "Media key Z-A"
                      : "Service scope A-Z"}
            </Select.Trigger>
            <Select.Content>
              <Select.Item value="seerr_asc" label="Seerr user A-Z">
                Seerr user A-Z
              </Select.Item>
              <Select.Item value="seerr_desc" label="Seerr user Z-A">
                Seerr user Z-A
              </Select.Item>
              <Select.Item value="media_asc" label="Media key A-Z">
                Media key A-Z
              </Select.Item>
              <Select.Item value="media_desc" label="Media key Z-A">
                Media key Z-A
              </Select.Item>
              <Select.Item value="service_asc" label="Service scope A-Z">
                Service scope A-Z
              </Select.Item>
            </Select.Content>
          </Select.Root>
        </div>
      </div>

      <div class="space-y-3">
        {#if requesterWatchUserMappings.length === 0}
          <p class="text-sm text-muted-foreground">
            No mappings yet. Add rows only when Seerr and media-server names
            differ.
          </p>
        {:else if visibleMappings.length === 0}
          <p class="text-sm text-muted-foreground">
            No mappings match the current filter.
          </p>
        {/if}

        {#each visibleMappings as row (`map-${row.index}`)}
          {@const mapping = row.mapping}
          <div
            class="rounded-md border border-border bg-background/40 p-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4"
          >
            <div class="min-w-0">
              <Label class="mb-2 text-xs whitespace-nowrap"
                >Seerr user (lookup)</Label
              >
              <Select.Root
                type="single"
                value={seerrSelectValue(mapping)}
                onValueChange={(value) => {
                  if (!value || value === "__manual__") {
                    mapping.seerr_user_id = null;
                    return;
                  }
                  const id = Number(value);
                  mapping.seerr_user_id = Number.isFinite(id) ? id : null;
                }}
              >
                <Select.Trigger
                  class="w-full min-w-0 cursor-pointer text-foreground"
                >
                  <span class="block truncate"
                    >{seerrTriggerLabel(mapping)}</span
                  >
                </Select.Trigger>
                <Select.Content>
                  <Select.Item value="__manual__" label="Manual username only">
                    Manual username only
                  </Select.Item>
                  {#each seerrUsers as user (user.id)}
                    <Select.Item
                      value={String(user.id)}
                      label={seerrLabel(user)}
                      class="cursor-pointer"
                    >
                      {seerrLabel(user)}
                    </Select.Item>
                  {/each}
                </Select.Content>
              </Select.Root>
            </div>

            <div class="min-w-0">
              <Label class="mb-2 text-xs whitespace-nowrap"
                >Seerr username (manual)</Label
              >
              <Input
                type="text"
                class="text-sm"
                placeholder="optional if ID selected"
                value={mapping.seerr_username ?? ""}
                oninput={(event) => {
                  const value = event.currentTarget.value.trim().toLowerCase();
                  mapping.seerr_username = value || null;
                }}
              />
            </div>

            <div class="min-w-0">
              <Label class="mb-2 text-xs whitespace-nowrap"
                >Media watch user key</Label
              >
              <Popover.Root
                open={isMediaUserPickerOpen(row.index)}
                onOpenChange={(open) =>
                  setMediaUserPickerOpen(
                    row.index,
                    open,
                    String(mapping.media_user_key ?? ""),
                  )}
              >
                <Popover.Trigger class="w-full">
                  <Button
                    type="button"
                    variant="outline"
                    class="w-full justify-between font-normal text-sm min-w-0"
                  >
                    <span class="truncate">
                      {mapping.media_user_key?.trim()
                        ? mapping.media_user_key
                        : "Select or enter media watch user key"}
                    </span>
                    <ChevronsUpDown class="size-4 shrink-0 opacity-60" />
                  </Button>
                </Popover.Trigger>
                <Popover.Content
                  align="start"
                  class="w-(--bits-popover-anchor-width) p-0 gap-0"
                >
                  <Command.Root shouldFilter={false} class="rounded-md p-0">
                    <Command.Input
                      bind:value={mediaUserQuery}
                      placeholder="Search users or service..."
                    />
                    <Command.List class="max-h-64">
                      <Command.Empty>No matching users found.</Command.Empty>
                      {#if mediaUserQuery.trim() && !mediaUserQueryMatchesKnownUser}
                        <Command.Item
                          value={`custom-${normalizeKey(mediaUserQuery)}`}
                          onSelect={() =>
                            selectMediaUserKey(row.index, mediaUserQuery)}
                        >
                          Use custom: {mediaUserQuery.trim()}
                        </Command.Item>
                      {/if}
                      {#each filteredWatchUsersForQuery as user (`${user.user_key_normalized}`)}
                        <Command.Item
                          value={`watch-user-${user.user_key_normalized}`}
                          keywords={user.source_services}
                          onSelect={() =>
                            selectMediaUserKey(row.index, user.user_key)}
                        >
                          <div class="min-w-0">
                            <p class="truncate text-sm">{user.user_key}</p>
                            <p
                              class="text-[11px] text-muted-foreground truncate"
                            >
                              {user.source_services.join(", ")}
                            </p>
                          </div>
                        </Command.Item>
                      {/each}
                    </Command.List>
                  </Command.Root>
                </Popover.Content>
              </Popover.Root>
            </div>

            <div class="min-w-0">
              <Label class="mb-2 text-xs whitespace-nowrap">Service scope</Label
              >
              <Select.Root
                type="single"
                value={serviceScopeValue(mapping)}
                onValueChange={(value) => {
                  mapping.service_type =
                    !value || value === "__any__" ? null : value;
                }}
              >
                <Select.Trigger
                  class="w-full min-w-0 cursor-pointer text-foreground"
                >
                  <span class="block truncate">
                    {serviceScopeLabel(mapping.service_type)}
                  </span>
                </Select.Trigger>
                <Select.Content>
                  <Select.Item value="__any__" label="Any service">
                    Any service
                  </Select.Item>
                  <Select.Item value="plex" label="Plex" class="cursor-pointer">
                    Plex
                  </Select.Item>
                  <Select.Item
                    value="jellyfin"
                    label="Jellyfin"
                    class="cursor-pointer"
                  >
                    Jellyfin
                  </Select.Item>
                  <Select.Item value="emby" label="Emby" class="cursor-pointer">
                    Emby
                  </Select.Item>
                </Select.Content>
              </Select.Root>
            </div>

            <div
              class="flex items-end md:col-span-2 md:justify-end xl:col-span-4"
            >
              <Button
                size="icon-sm"
                type="button"
                class="w-full sm:w-9 bg-destructive/70 hover:bg-destructive/80 cursor-pointer"
                onclick={() => removeMapping(row.index)}
                aria-label="Remove mapping"
              >
                <Trash2 class="size-4" />
              </Button>
            </div>
          </div>
        {/each}
      </div>

      <div
        class="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between"
      >
        <Button
          size="sm"
          type="button"
          class="cursor-pointer gap-2"
          onclick={addMapping}
        >
          <Plus class="size-4" />
          Add mapping
        </Button>
        <p class="text-xs text-muted-foreground">
          Known watch keys: {watchUsers.length}
        </p>
      </div>
    </div>

    <div class="flex justify-end">
      <Button
        onclick={saveSettings}
        disabled={savingSettings || loadError !== null}
        class="cursor-pointer gap-2"
      >
        {#if savingSettings}
          <Spinner class="size-4" />
        {:else}
          <Save class="size-4" />
        {/if}
        Save
      </Button>
    </div>
  {/if}
</div>
