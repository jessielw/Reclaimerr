/** @param {unknown} value @returns {string} */
export function evidenceValue(value) {
  if (value === null || value === undefined) return "No value recorded";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  if (Array.isArray(value))
    return value.length ? value.map(evidenceValue).join(", ") : "Empty list";
  if (typeof value === "object")
    return Object.entries(value)
      .map(
        ([key, item]) => `${key.replaceAll("_", " ")}: ${evidenceValue(item)}`,
      )
      .join("; ");
  return "Unavailable";
}

/** @param {string} value */
export function outcomeLabel(value) {
  return (
    {
      matched: "Matched",
      not_matched: "Did not match",
      not_evaluated: "Not evaluated",
      unknown: "Unknown",
      succeeded: "Succeeded",
      failed: "Failed",
      deferred: "Deferred",
    }[value] ?? value.replaceAll("_", " ")
  );
}

/** @param {{scope: string, id: number} | null | undefined} target */
export function targetLabel(target) {
  return target
    ? `${target.scope.replaceAll("_", " ")} #${target.id}`
    : "System";
}

/** @param {Pick<import('$lib/types/diagnostics').TraceCondition, 'availability' | 'actual'>} condition */
export function observedValue(condition) {
  if (condition.availability === "not_evaluated") return "Not evaluated";
  if (condition.availability === "unknown") return "Unavailable at evaluation";
  return evidenceValue(condition.actual);
}

/** @param {unknown} node @param {number} depth @returns {string} */
export function ruleLogic(node, depth = 0) {
  if (depth > 10 || !node || typeof node !== "object")
    return typeof node === "string" ? node : "Unavailable rule structure";
  if ("root" in node) return ruleLogic(node.root, depth);
  const indent = "  ".repeat(depth);
  const disabled =
    "enabled" in node && node.enabled === false ? "[disabled] " : "";
  if ("conditions" in node && Array.isArray(node.conditions)) {
    const logic =
      "logic" in node ? String(node.logic).toLowerCase() : "unknown";
    const label =
      logic === "and" ? "ALL of" : logic === "or" ? "ANY of" : "Unknown group";
    return `${indent}${disabled}${label}:\n${node.conditions.map((child) => ruleLogic(child, depth + 1)).join("\n")}${"truncated" in node && node.truncated ? `\n${indent}  More conditions omitted` : ""}`;
  }
  return `${indent}${disabled}${"field" in node ? String(node.field) : "Unknown field"} ${"operator" in node ? String(node.operator).replaceAll("_", " ") : ""} ${"value" in node ? evidenceValue(node.value) : ""}`.trimEnd();
}
