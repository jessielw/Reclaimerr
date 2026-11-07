import assert from "node:assert/strict";
import { test } from "node:test";
import {
  scheduledDeletionTime,
  scheduledDeletionError,
  localDateTime,
} from "../src/lib/utils/scheduled-deletion-time.js";

process.env.TZ = "America/New_York";

test("converts the selected local time to UTC using its date's DST offset", () => {
  assert.equal(
    scheduledDeletionTime("2099-07-01T12:30", 0),
    "2099-07-01T16:30:00.000Z",
  );
  assert.equal(
    scheduledDeletionTime("2099-01-01T12:30", 0),
    "2099-01-01T17:30:00.000Z",
  );
});

test("rejects past times, missing values, invalid dates, and DST gaps", () => {
  for (const value of ["", "garbage", "2026-02-30T12:00", "2026-03-08T02:30"]) {
    assert.throws(() => scheduledDeletionTime(value, 0));
  }
  assert.throws(() => scheduledDeletionTime("2020-01-01T12:00"), /future/);
  assert.ok(scheduledDeletionError(""));
});

test("round-trips an ambiguous fall-back time with an explicit displayed instant", () => {
  const value = "2026-11-01T01:30";
  assert.equal(localDateTime(new Date(scheduledDeletionTime(value, 0))), value);
  assert.match(scheduledDeletionTime(value, 0), /Z$/);
});
