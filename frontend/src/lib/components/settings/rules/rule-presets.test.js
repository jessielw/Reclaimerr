import assert from "node:assert/strict";
import test from "node:test";
import { createPresetDraft } from "./rule-presets.js";

/** @type {import('$lib/types/shared').MediaType} */
const SERIES = /** @type {import('$lib/types/shared').MediaType} */ ("series");
const MOVIE = /** @type {import('$lib/types/shared').MediaType} */ ("movie");

/** @returns {import('$lib/types/shared').RulePreset} */
const template = () => ({
  id: "test-preset",
  version: 1,
  title: "Test preset",
  description: "Example",
  prerequisites: [],
  required_services: [],
  target_scopes: ["season"],
  required_inputs: ["library.id", "playback.fully_watched_usernames"],
  rule: {
    name: "Example",
    description: "Example",
    media_type: SERIES,
    target_scope: "season",
    enabled: false,
    action: {
      outcome: "candidate",
      candidate: true,
      tag_enabled: false,
      arr_tag: null,
      arr_action: "delete",
      media_server_action: "delete",
      auto_delete_enabled: false,
      auto_delete_delay_days: null,
      move_instead_of_delete: false,
      quality_profile_id: null,
      trigger_search: false,
      radarr_service_config_id: null,
      sonarr_service_config_id: null,
      radarr_service_config_ids: [],
      sonarr_service_config_ids: [],
    },
    definition: {
      version: 1,
      root: {
        type: "group",
        op: "and",
        children: [
          {
            type: "condition",
            field: "library.id",
            operator: "contains_any",
            value: [],
          },
          {
            type: "condition",
            field: "playback.fully_watched_usernames",
            operator: "contains_all",
            value: [],
          },
          {
            type: "condition",
            field: "season.season_number",
            operator: "greater_than",
            value: 0,
          },
        ],
      },
    },
  },
});
const inputs = () => ({
  libraryIds: ["tv"],
  usernames: ["alice"],
  libraries: [
    { mediaType: SERIES, libraryId: "tv" },
    { mediaType: MOVIE, libraryId: "movies" },
  ],
  users: [{ username: "alice" }, { username: "bob" }],
});

/** @param {import('$lib/types/shared').RuleDraft} draft @param {number} index */
function conditionAt(draft, index) {
  assert.ok(draft.definition);
  const node = draft.definition.root.children[index];
  assert.equal(node.type, "condition");
  if (node.type !== "condition") throw new Error("Expected a condition");
  return node;
}

test("opening/editing/canceling a draft leaves catalog and future drafts untouched", () => {
  const preset = template();
  const original = structuredClone(preset);
  const draft = createPresetDraft(preset, inputs());
  assert.equal(draft.enabled, false);
  assert.ok(draft.action);
  assert.equal(draft.action.auto_delete_enabled, false);
  assert.equal(draft.action.move_instead_of_delete, false);
  assert.deepEqual(conditionAt(draft, 0).value, ["tv"]);
  assert.deepEqual(conditionAt(draft, 1).value, ["alice"]);
  draft.name = "My policy";
  conditionAt(draft, 2).value = 10;
  assert.deepEqual(preset, original);
  assert.equal(conditionAt(createPresetDraft(preset, inputs()), 2).value, 0);
  assert.equal("id" in draft, false);
});

test("missing, stale and wrong-type library selections cannot broaden the draft", () => {
  for (const libraryIds of [[], ["missing"], ["movies"], ["tv", "missing"]]) {
    assert.throws(
      () => createPresetDraft(template(), { ...inputs(), libraryIds }),
      /library/,
    );
  }
});

test("user prerequisites cannot be dropped or substituted", () => {
  for (const usernames of [[], ["unknown"], ["alice", "unknown"]]) {
    assert.throws(
      () => createPresetDraft(template(), { ...inputs(), usernames }),
      /user/,
    );
  }
  assert.throws(
    () => createPresetDraft(template(), { ...inputs(), users: [] }),
    /user/,
  );
  const preset = template();
  assert.ok(preset.rule.definition);
  preset.rule.definition.root.children.splice(1, 1);
  assert.throws(
    () => createPresetDraft(preset, inputs()),
    /required condition/,
  );
});

test("movie presets do not require a playback integration or users", () => {
  const preset = template();
  preset.required_inputs = ["library.id"];
  preset.rule.media_type = MOVIE;
  preset.rule.target_scope = "movie_version";
  preset.target_scopes = ["movie_version"];
  assert.ok(preset.rule.definition);
  preset.rule.definition.root.children =
    preset.rule.definition.root.children.slice(0, 1);
  const draft = createPresetDraft(preset, {
    ...inputs(),
    libraryIds: ["movies", "movies"],
    users: [],
    usernames: [],
  });
  assert.deepEqual(conditionAt(draft, 0).value, ["movies"]);
});

test("favorites protection can target shows without enabling cleanup", () => {
  const preset = template();
  preset.required_inputs = ["library.id"];
  preset.target_scopes = ["movie_version", "series"];
  preset.rule.target_scope = "movie_version";
  preset.rule.media_type = MOVIE;
  assert.ok(preset.rule.action);
  preset.rule.action.outcome = "protect";
  preset.rule.action.candidate = false;
  preset.rule.action.media_server_action = null;
  const draft = createPresetDraft(preset, {
    ...inputs(),
    targetScope: "series",
  });
  assert.equal(draft.target_scope, "series");
  assert.equal(draft.media_type, SERIES);
  assert.equal(draft.action?.outcome, "protect");
  assert.equal(draft.action?.candidate, false);
  assert.equal(draft.action?.media_server_action, null);
  assert.equal(draft.enabled, false);
  assert.throws(
    () =>
      createPresetDraft(preset, {
        ...inputs(),
        targetScope: "series",
        libraryIds: ["movies"],
      }),
    /library/,
  );
  assert.throws(
    () => createPresetDraft(preset, { ...inputs(), targetScope: "episode" }),
    /supported target/,
  );
});

test("missing integrations block drafts without dropping conditions", () => {
  for (const service of /** @type {const} */ (["sonarr", "seerr"])) {
    const preset = template();
    preset.required_services = [service];
    const original = structuredClone(preset);
    assert.throws(
      () => createPresetDraft(preset, inputs()),
      /Configure and enable/,
    );
    assert.throws(
      () =>
        createPresetDraft(preset, { ...inputs(), availableServices: ["plex"] }),
      /Configure and enable/,
    );
    const draft = createPresetDraft(preset, {
      ...inputs(),
      availableServices: [service],
    });
    assert.equal(draft.definition?.root.children.length, 3);
    assert.deepEqual(preset, original);
  }
});
