import assert from "node:assert/strict";
import test from "node:test";

import { isConditionValueSet } from "./rule-condition-value.js";

/** @typedef {import("$lib/types/shared").RuleCondition} RuleCondition */

/**
 * @param {Partial<RuleCondition>} overrides
 * @returns {RuleCondition}
 */
const condition = (overrides = {}) => ({
  type: "condition",
  field: "media.size",
  operator: "greater_than",
  ...overrides,
});

test("a valueless operator needs no value", () => {
  assert.equal(
    isConditionValueSet(condition({ operator: "exists", value: null })),
    true,
  );
});

test("a scalar value counts only when it is not blank", () => {
  assert.equal(isConditionValueSet(condition({ value: 30 })), true);
  assert.equal(isConditionValueSet(condition({ value: 0 })), true);
  assert.equal(isConditionValueSet(condition({ value: "  " })), false);
  assert.equal(isConditionValueSet(condition({ value: null })), false);
});

test("a list value counts when any entry is filled in", () => {
  assert.equal(isConditionValueSet(condition({ value: ["", "sci-fi"] })), true);
  assert.equal(isConditionValueSet(condition({ value: ["", " "] })), false);
  assert.equal(isConditionValueSet(condition({ value: [] })), false);
});

test("a user-scoped playback value needs both a user and an amount", () => {
  const field = "playback.user_watched_duration_minutes";
  assert.equal(
    isConditionValueSet(
      condition({ field, value: { usernames: ["alice"], amount: 30 } }),
    ),
    true,
  );
  assert.equal(
    isConditionValueSet(
      condition({ field, value: { usernames: [], amount: 30 } }),
    ),
    false,
  );
  assert.equal(
    isConditionValueSet(
      condition({ field, value: { usernames: ["alice"], amount: null } }),
    ),
    false,
  );
  assert.equal(
    isConditionValueSet(
      condition({ field, value: { usernames: ["  "], amount: 30 } }),
    ),
    false,
  );
});
