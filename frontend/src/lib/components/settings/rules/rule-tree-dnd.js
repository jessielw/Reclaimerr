/**
 * @typedef {import("$lib/types/shared").RuleGroup} RuleGroup
 * @typedef {import("$lib/types/shared").RuleNode} RuleNode
 */

/**
 * Return the greatest group-to-descendant-group distance in a group subtree.
 * A group without nested groups has a distance of zero.
 *
 * @param {RuleGroup} group
 * @returns {number}
 */
export function nestedGroupDistance(group) {
  let greatestDistance = 0;
  for (const child of group.children) {
    if (child.type !== "group") continue;
    greatestDistance = Math.max(
      greatestDistance,
      1 + nestedGroupDistance(child),
    );
  }
  return greatestDistance;
}

/**
 * Check whether target is the ancestor itself or one of its nested groups.
 *
 * @param {RuleGroup} ancestor
 * @param {RuleGroup} target
 * @returns {boolean}
 */
export function groupContainsGroup(ancestor, target) {
  if (ancestor === target) return true;
  return ancestor.children.some(
    (child) =>
      child.type === "group" &&
      (child === target || groupContainsGroup(child, target)),
  );
}

/**
 * Determine whether a rule node can be moved into a target group.
 *
 * @param {RuleNode | null} draggedNode
 * @param {RuleGroup} targetGroup
 * @param {number} targetDepth
 * @param {number} maxGroupDepth
 * @returns {boolean}
 */
export function canMoveRuleNodeToGroup(
  draggedNode,
  targetGroup,
  targetDepth,
  maxGroupDepth,
) {
  if (draggedNode === null || draggedNode.type === "condition") return true;
  if (groupContainsGroup(draggedNode, targetGroup)) return false;

  const movedRootDepth = targetDepth + 1;
  return movedRootDepth + nestedGroupDistance(draggedNode) <= maxGroupDepth;
}
