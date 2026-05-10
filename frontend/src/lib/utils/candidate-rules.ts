import type { ReclaimCandidateEntry } from "$lib/types/shared";

export const parseRuleTokens = (
  tokens: string[] | null | undefined,
): string[] => (tokens ?? []).map((token) => token.trim()).filter(Boolean);

export const unique = (values: string[]): string[] => [...new Set(values)];

export const ruleNames = (entry: ReclaimCandidateEntry): string[] => {
  const fromParts = unique(
    (entry.reason_parts ?? [])
      .map((part) => part.rule_name?.trim() ?? "")
      .filter(Boolean),
  );
  if (fromParts.length > 0) return fromParts;
  return unique(
    parseRuleTokens(entry.reason_tokens)
      .map((token) => token.split(":")[0]?.trim() ?? "")
      .filter(Boolean),
  );
};

export const detailReasons = (entry: ReclaimCandidateEntry): string[] => {
  const details = (entry.reason_parts ?? [])
    .map((part) => part.text?.trim() ?? "")
    .filter(Boolean);
  if (details.length > 0) return details;
  return parseRuleTokens(entry.reason_tokens);
};

export const rulePreview = (entry: ReclaimCandidateEntry): string[] =>
  ruleNames(entry).slice(0, 2);

export const extraRuleCount = (entry: ReclaimCandidateEntry): number =>
  Math.max(0, ruleNames(entry).length - 2);

export const groupRuleNames = (entries: ReclaimCandidateEntry[]): string[] =>
  unique(entries.flatMap((entry) => ruleNames(entry)));

export const extractPathNoFile = (path: string): string => {
  const parts = path.split(/[/\\]/);
  parts.pop();
  return parts.join("/");
};

export const fileNameFromPath = (
  path: string | null,
  fallbackFileName: string | null = null,
): string => {
  if (fallbackFileName && fallbackFileName.trim()) {
    return fallbackFileName.trim();
  }
  if (!path) return "Unknown";
  const parts = path.split(/[/\\]/);
  const extractedFileName = parts[parts.length - 1];
  return extractedFileName?.trim() ? extractedFileName : "Unknown";
};
