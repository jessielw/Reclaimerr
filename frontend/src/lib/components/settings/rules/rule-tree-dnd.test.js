import assert from "node:assert/strict";
import test from "node:test";

import {
  canMoveRuleNodeToGroup,
  groupContainsGroup,
  nestedGroupDistance,
} from "./rule-tree-dnd.js";

/** @typedef {import("$lib/types/shared").RuleCondition} RuleCondition */
/** @typedef {import("$lib/types/shared").RuleGroup} RuleGroup */
/** @typedef {import("$lib/types/shared").RuleNode} RuleNode */

/**
 * @param {string} field
 * @returns {RuleCondition}
 */
const condition = (field = "media.size") => ({
  type: "condition",
  field,
  operator: "greater_than",
  value: 1,
});

/**
 * @param {...RuleNode} children
 * @returns {RuleGroup}
 */
const group = (...children) => ({
  type: "group",
  op: "and",
  children,
});

test("conditions can move into any group depth", () => {
  assert.equal(canMoveRuleNodeToGroup(condition(), group(), 4, 4), true);
});

test("groups cannot move into themselves or descendants", () => {
  const descendant = group(condition());
  const dragged = group(descendant);

  assert.equal(groupContainsGroup(dragged, dragged), true);
  assert.equal(groupContainsGroup(dragged, descendant), true);
  assert.equal(canMoveRuleNodeToGroup(dragged, dragged, 1, 4), false);
  assert.equal(canMoveRuleNodeToGroup(dragged, descendant, 2, 4), false);
});

test("group moves respect the maximum resulting subtree depth", () => {
  const nested = group(group(group(condition())));

  assert.equal(nestedGroupDistance(nested), 2);
  assert.equal(canMoveRuleNodeToGroup(nested, group(), 1, 4), true);
  assert.equal(canMoveRuleNodeToGroup(nested, group(), 2, 4), false);
});

test("groups can move into unrelated empty groups", () => {
  assert.equal(canMoveRuleNodeToGroup(group(condition()), group(), 3, 4), true);
});
