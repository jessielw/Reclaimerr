import assert from "node:assert/strict";
import test from "node:test";

import {
  ruleFieldSearchKeywords,
  scoreRuleFieldSearch,
} from "./rule-field-search.js";

/**
 * @param {string} value
 * @param {string} label
 * @param {string} category
 * @param {string} query
 */
const score = (value, label, category, query) =>
  scoreRuleFieldSearch(
    value,
    query,
    ruleFieldSearchKeywords(value, label, category),
  );

test("finds a Rotten Tomatoes field despite a small spelling error", () => {
  assert.ok(
    score(
      "rottentomatoes.tomato_meter",
      "Rotten Tomatoes Tomatometer",
      "Common",
      "rotton tomatoes",
    ) > 0,
  );
});

test("keeps provider context for fields displayed in Common", () => {
  assert.ok(
    score(
      "rottentomatoes.popcorn_meter",
      "Rotten Tomatoes Popcornmeter",
      "Common",
      "audience score",
    ) > 0,
  );
});

test("ranks an exact multi-word field match above a partial match", () => {
  const seriesStatus = score(
    "series.status",
    "Series status",
    "Common",
    "series status",
  );
  const seerrRequest = score(
    "seerr.requested",
    "Seerr requested",
    "Seerr",
    "series status",
  );

  assert.ok(seriesStatus > 0);
  assert.equal(seerrRequest, 0);
});

test("keeps strong partial matches visible for blended searches", () => {
  assert.ok(
    score("series.status", "Series status", "Common", "seerr series status") >
      0,
  );
});

test("supports common Seerr product names as aliases", () => {
  assert.ok(
    score("seerr.requested", "Seerr requested", "Seerr", "overseerr request") >
      0,
  );
});
