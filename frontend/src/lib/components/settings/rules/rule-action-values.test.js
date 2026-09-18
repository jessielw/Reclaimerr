import assert from "node:assert/strict";
import test from "node:test";

import { parseAutoDeleteDelay } from "./rule-action-values.js";

test("a cleared auto-delete delay uses the server default", () => {
  assert.equal(parseAutoDeleteDelay(undefined), null);
  assert.equal(parseAutoDeleteDelay(null), null);
  assert.equal(parseAutoDeleteDelay(""), null);
  assert.equal(parseAutoDeleteDelay("  "), null);
});

test("a populated auto-delete delay is converted to a number", () => {
  assert.equal(parseAutoDeleteDelay("0"), 0);
  assert.equal(parseAutoDeleteDelay("14"), 14);
  assert.equal(parseAutoDeleteDelay(30), 30);
});
