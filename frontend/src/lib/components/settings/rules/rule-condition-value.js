/**
 * @typedef {import("$lib/types/shared").RuleCondition} RuleCondition
 * @typedef {import("$lib/types/shared").RuleConditionOperator} RuleConditionOperator
 * @typedef {import("$lib/types/shared").UserScopedPlaybackValue} UserScopedPlaybackValue
 */

/**
 * Operators that carry no value of their own.
 *
 * @type {Set<RuleConditionOperator>}
 */
export const valuelessOperators = new Set([
  "exists",
  "not_exists",
  "is_true",
  "is_false",
]);

/**
 * Whether a condition carries the value its operator needs.
 *
 * Object values belong to the user-scoped playback fields, whose value is a
 * `{usernames, amount}` pair rather than a scalar. They are recognised by shape
 * rather than by field name, and both halves have to be filled in: stringifying
 * the object instead would read `[object Object]` and accept a condition with
 * no user picked, which the backend then rejects on save.
 *
 * @param {RuleCondition} condition
 * @returns {boolean}
 */
export function isConditionValueSet(condition) {
  if (valuelessOperators.has(condition.operator)) return true;
  const value = condition.value;
  if (Array.isArray(value)) {
    return value.some(
      (item) =>
        item !== null && item !== undefined && String(item).trim() !== "",
    );
  }
  if (value === null || value === undefined) return false;
  if (typeof value === "object") {
    const { usernames, amount } = /** @type {UserScopedPlaybackValue} */ (
      value
    );
    return (
      Array.isArray(usernames) &&
      usernames.some((name) => String(name).trim() !== "") &&
      typeof amount === "number" &&
      Number.isFinite(amount)
    );
  }
  return String(value).trim() !== "";
}
