import type { RuleDefinition, RuleOutcome } from "$lib/types/shared";

export type RuleTargetScope = "movie_version" | "series" | "season" | "episode";
export type RuleCheckState = "pass" | "fail" | "unknown" | "disabled";
export type RuleExplainTarget = {
  id: number;
  label: string;
  path: string | null;
};
export type RuleExplainTargets = {
  items: RuleExplainTarget[];
  total: number;
  page: number;
  per_page: number;
};
export type RuleExplainNode = {
  type: "group" | "condition";
  state: RuleCheckState;
  op: string | null;
  field: string | null;
  label: string | null;
  operator: string | null;
  operator_label: string | null;
  expected: unknown;
  actual: unknown;
  children: RuleExplainNode[];
};
export type RuleExplainSnapshot = {
  name: string | null;
  target_scope: RuleTargetScope;
  definition: RuleDefinition;
  outcome: RuleOutcome;
};
export type RuleExplanation = {
  target: RuleExplainTarget;
  target_scope: RuleTargetScope;
  outcome: RuleOutcome;
  condition_state: RuleCheckState;
  eligible: boolean;
  exclusions: string[];
  warnings: string[];
  root: RuleExplainNode;
};
