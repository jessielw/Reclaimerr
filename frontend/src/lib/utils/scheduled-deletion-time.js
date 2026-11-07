/** Convert a local picker value to an explicit instant; reject DST gaps.
 * @param {string} value
 * @param {number} [now]
 */
export function scheduledDeletionTime(value, now = Date.now()) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) {
    throw new Error("Choose a deletion date and time.");
  }
  const date = new Date(value);
  if (!Number.isFinite(date.getTime()) || localDateTime(date) !== value) {
    throw new Error(
      "Choose a valid local time. This time may be skipped by daylight saving time.",
    );
  }
  if (date.getTime() <= now)
    throw new Error("Deletion time must be in the future.");
  return date.toISOString();
}

/** @param {Date} date */
export function localDateTime(date) {
  /** @param {number} value */
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** @param {string} value */
export function scheduledDeletionError(value) {
  try {
    scheduledDeletionTime(value);
    return "";
  } catch (error) {
    return error instanceof Error
      ? error.message
      : "Choose a valid deletion time.";
  }
}
