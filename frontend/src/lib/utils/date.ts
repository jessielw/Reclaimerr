import { get } from "svelte/store";

import { dateFormat, type DateFormat } from "$lib/stores/date-format";

/**
 * Parse API timestamps, treating timezone-less ISO strings as UTC.
 * SQLite-backed timestamps often arrive without a timezone suffix.
 */
const parseApiDate = (dateString: string): Date => {
  const hasTimezone = /[zZ]|[+-]\d{2}:\d{2}$/.test(dateString);
  return new Date(hasTimezone ? dateString : `${dateString}Z`);
};

/**
 * Formats a date string using the selected display format.
 * @param dateString The date string to format.
 * @returns A formatted date string.
 */
const formatDateValue = (date: Date, format: DateFormat): string => {
  const year = date.getFullYear().toString().padStart(4, "0");
  const month = (date.getMonth() + 1).toString().padStart(2, "0");
  const day = date.getDate().toString().padStart(2, "0");

  switch (format) {
    case "dmy":
      return `${day}/${month}/${year}`;
    case "iso":
      return `${year}-${month}-${day}`;
    case "mdy":
      return `${month}/${day}/${year}`;
  }
};

/** Formats a date using the signed-in user's display preference. */
const formatDate = (dateString: string): string => {
  try {
    return formatDateValue(parseApiDate(dateString), get(dateFormat));
  } catch {
    return dateString;
  }
};

/**
 * Formats a date string into a relative time format (e.g., "2 days ago" or "in 3 hours").
 * @param dateString The date string to format.
 * @returns A formatted relative time string.
 */
const formatDistanceToNow = (dateString: string): string => {
  const date = parseApiDate(dateString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const isFuture = diffMs < 0;
  const absDiffMs = Math.abs(diffMs);

  const seconds = Math.floor(absDiffMs / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  let result = "";
  if (days > 0) {
    result = `${days} day${days > 1 ? "s" : ""}`;
  } else if (hours > 0) {
    result = `${hours} hour${hours > 1 ? "s" : ""}`;
  } else if (minutes > 0) {
    result = `${minutes} minute${minutes > 1 ? "s" : ""}`;
  } else {
    result = `${seconds} second${seconds > 1 ? "s" : ""}`;
  }

  return isFuture ? `in ${result}` : `${result} ago`;
};

/**
 * Formats a date string into a locale-specific date string. If the input is null, it returns "Unknown".
 * @param dateStr The date string to format.
 * @returns A formatted date string in the locale-specific format, or "Unknown" if the input is null.
 */
const formatDateToLocaleString = (dateStr: string | null): string => {
  if (!dateStr) return "Unknown";
  return formatDate(dateStr);
};

/**
 * Formats a date string using the selected date format and browser time format.
 * Timezone-less API strings are treated as UTC.
 */
const formatDateTimeToLocaleString = (dateStr: string | null): string => {
  if (!dateStr) return "Unknown";
  try {
    const date = parseApiDate(dateStr);
    const time = new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
    }).format(date);
    return `${formatDateValue(date, get(dateFormat))} ${time}`;
  } catch {
    return dateStr;
  }
};

export {
  formatDate,
  formatDistanceToNow,
  formatDateToLocaleString,
  formatDateTimeToLocaleString,
};
