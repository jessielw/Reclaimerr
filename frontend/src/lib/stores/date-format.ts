import { writable } from "svelte/store";

export const DATE_FORMAT_STORAGE_KEY = "reclaimerr-date-format";

export const DATE_FORMATS = ["mdy", "dmy", "iso"] as const;

export type DateFormat = (typeof DATE_FORMATS)[number];

const VALID_DATE_FORMATS = new Set<DateFormat>(DATE_FORMATS);
const hasStorage = typeof window !== "undefined";

function normalizeDateFormat(value: string | null | undefined): DateFormat {
  return value && VALID_DATE_FORMATS.has(value as DateFormat)
    ? (value as DateFormat)
    : "mdy";
}

function readDateFormat(): DateFormat {
  if (!hasStorage) return "mdy";

  try {
    return normalizeDateFormat(
      window.localStorage.getItem(DATE_FORMAT_STORAGE_KEY),
    );
  } catch {
    return "mdy";
  }
}

export const dateFormat = writable<DateFormat>(readDateFormat());

if (hasStorage) {
  dateFormat.subscribe((value) => {
    try {
      window.localStorage.setItem(
        DATE_FORMAT_STORAGE_KEY,
        normalizeDateFormat(value),
      );
    } catch {
      // The preference remains active for the current session.
    }
  });
}

export function setDateFormat(value: DateFormat) {
  dateFormat.set(normalizeDateFormat(value));
}
