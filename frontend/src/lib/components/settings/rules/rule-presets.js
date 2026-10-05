/**
 * Bind installation inputs to a fresh catalog template. No persistence occurs.
 * @param {import('$lib/types/shared').RulePreset} preset
 * @param {{libraryIds: string[], usernames: string[], libraries: Pick<import('$lib/types/shared').LibraryType, 'libraryId' | 'mediaType'>[], users: {username: string}[], targetScope?: NonNullable<import('$lib/types/shared').RuleDraft['target_scope']>, availableServices?: string[]}} inputs
 * @returns {import('$lib/types/shared').RuleDraft}
 */
export function createPresetDraft(preset, inputs) {
  const targetScope = inputs.targetScope ?? preset.rule.target_scope;
  if (!targetScope || !preset.target_scopes.includes(targetScope)) {
    throw new Error("Choose a supported target for this preset.");
  }
  for (const service of preset.required_services) {
    if (!inputs.availableServices?.includes(service)) {
      throw new Error(
        `Configure and enable ${service === "sonarr" ? "Sonarr" : "Seerr"} before using this preset.`,
      );
    }
  }
  const mediaType = /** @type {import('$lib/types/shared').MediaType} */ (
    targetScope === "movie_version" ? "movie" : "series"
  );
  const libraryIds = [...new Set(inputs.libraryIds)];
  const usernames = [...new Set(inputs.usernames)];
  const availableIds = new Set(
    inputs.libraries
      .filter((library) => library.mediaType === mediaType)
      .map((library) => library.libraryId),
  );
  if (!libraryIds.length || libraryIds.some((id) => !availableIds.has(id))) {
    throw new Error("Choose at least one available library for this preset.");
  }
  if (preset.required_inputs.includes("playback.fully_watched_usernames")) {
    const availableUsers = new Set(inputs.users.map((user) => user.username));
    if (
      !usernames.length ||
      usernames.some((name) => !availableUsers.has(name))
    ) {
      throw new Error("Choose at least one user with imported watch data.");
    }
  }
  // JSON also clones Svelte proxies; catalog updates cannot mutate this draft.
  /** @type {import('$lib/types/shared').RuleDraft} */
  const draft = JSON.parse(JSON.stringify(preset.rule));
  draft.target_scope = targetScope;
  draft.media_type = mediaType;
  if (!draft.definition) throw new Error("This preset has no conditions.");
  for (const field of preset.required_inputs) {
    const conditions = draft.definition.root.children.filter(
      (node) => node.type === "condition" && node.field === field,
    );
    if (conditions.length !== 1 || conditions[0].type !== "condition") {
      throw new Error("This preset is missing a required condition.");
    }
    conditions[0].value = field === "library.id" ? libraryIds : usernames;
  }
  return draft;
}
