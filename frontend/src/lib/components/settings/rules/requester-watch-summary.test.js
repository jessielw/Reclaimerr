import assert from "node:assert/strict";
import test from "node:test";

import { movieRequesterWatchSummary } from "./requester-watch-summary.js";

const requester = (overrides = {}) => ({
  movie_watched_at: null,
  movie_watched_before_request: false,
  ...overrides,
});

test("an unwatched movie requester is not described as having watched", () => {
  assert.deepEqual(movieRequesterWatchSummary(requester(), false), {
    text: "No completed movie watch found for this requester.",
    warning: true,
  });
});

test("a completed movie watch uses movie wording", () => {
  assert.deepEqual(
    movieRequesterWatchSummary(
      requester({ movie_watched_at: "2026-07-02T00:00:00Z" }),
      false,
    ),
    {
      text: "Watched the movie, after requesting it.",
      warning: false,
    },
  );
});

test("a movie watched before its request reports the date gate", () => {
  const watchedBefore = requester({
    movie_watched_at: "2026-01-02T00:00:00Z",
    movie_watched_before_request: true,
  });
  assert.equal(
    movieRequesterWatchSummary(watchedBefore, false).text,
    "Watched the movie, but only before requesting it.",
  );
  assert.equal(
    movieRequesterWatchSummary(watchedBefore, true).text,
    "Watched the movie before requesting it - still counted.",
  );
});

test("an early movie watch is no warning for a rule that ignores request dates", () => {
  const watchedBefore = requester({
    movie_watched_at: "2026-01-02T00:00:00Z",
    movie_watched_before_request: true,
  });
  assert.deepEqual(movieRequesterWatchSummary(watchedBefore, false, false), {
    text: "Watched the movie before requesting it - not used by this rule.",
    warning: false,
  });
  assert.equal(
    movieRequesterWatchSummary(watchedBefore, false, true).warning,
    true,
  );
});

test("a missing movie watch stays a warning whichever half the rule asks", () => {
  for (const gatedHalfUsed of [true, false]) {
    assert.equal(
      movieRequesterWatchSummary(requester(), false, gatedHalfUsed).warning,
      true,
    );
  }
});
