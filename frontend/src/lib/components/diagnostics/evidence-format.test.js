import assert from "node:assert/strict";
import test from "node:test";
import {
  evidenceValue,
  observedValue,
  outcomeLabel,
  ruleLogic,
} from "./evidence-format.js";

test("zero and false stay distinct from absent or unavailable evidence", () => {
  assert.equal(evidenceValue(0), "0");
  assert.equal(evidenceValue(false), "No");
  assert.equal(evidenceValue(null), "No value recorded");
  assert.equal(
    observedValue({ actual: 0, availability: "unknown" }),
    "Unavailable at evaluation",
  );
  assert.equal(
    observedValue({ actual: 20, availability: "not_evaluated" }),
    "Not evaluated",
  );
  assert.equal(outcomeLabel("not_matched"), "Did not match");
});

test("compound user thresholds remain readable without JSON", () => {
  assert.equal(
    evidenceValue({ usernames: ["user-1", "user-2"], amount: 30 }),
    "usernames: user-1, user-2; amount: 30",
  );
});

test("rule logic preserves nested ANY/ALL and disabled branches", () => {
  const text = ruleLogic({
    root: {
      logic: "and",
      conditions: [
        {
          logic: "or",
          conditions: [
            { field: "size", operator: "greater_than", value: 100 },
            { field: "watched", operator: "is_true", enabled: false },
          ],
        },
      ],
    },
  });
  assert.match(text, /ALL of:/);
  assert.match(text, /  ANY of:/);
  assert.match(text, /size greater than 100/);
  assert.match(text, /\[disabled\] watched is true/);
});
