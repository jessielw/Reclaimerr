/**
 * Parse an optional auto-delete delay from a numeric input binding.
 *
 * Svelte sets a cleared `type="number"` input to `undefined`, so nullish and
 * blank values must be normalized before converting populated values to a
 * number.
 *
 * @param {string | number | null | undefined} value
 * @returns {number | null}
 */
export function parseAutoDeleteDelay(value) {
  const rawValue = value == null ? "" : String(value).trim();
  return rawValue === "" ? null : Number(rawValue);
}
