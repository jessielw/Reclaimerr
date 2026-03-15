/**
 * Converts a string to title case, where the first letter of each word is capitalized and the rest are lowercase.
 * @param str
 * @returns
 */
const toTitleCase = (str: string, splitChar: string = " "): string => {
  return str
    .toLowerCase()
    .split(splitChar)
    .map((word: string) => {
      if (word.length === 0) return "";
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(" ");
};

/**
 * Truncates a string to a specified length and adds "..." if it exceeds that length.
 * @param str
 * @param num
 * @returns
 */
const truncateString = (str: string, num: number): string => {
  if (str.length <= num) {
    return str;
  }
  // check if num is less than or equal to 3, as '...' also takes up space
  const sliced = str.slice(0, num > 3 ? num - 3 : num);
  return sliced + "...";
};

/**
 * Formats an interval given in seconds into a human-readable string (e.g., "1h 30m 45s").
 * @param seconds
 * @returns
 */
const formatIntervalDisplay = (seconds: string): string => {
  const num = parseInt(seconds);
  if (isNaN(num)) return seconds;

  const hours = Math.floor(num / 3600);
  const minutes = Math.floor((num % 3600) / 60);
  const secs = num % 60;

  if (hours > 0) {
    return `${hours}h ${minutes > 0 ? minutes + "m " : ""}${secs > 0 ? secs + "s" : ""}`.trim();
  } else if (minutes > 0) {
    return `${minutes}m ${secs > 0 ? secs + "s" : ""}`.trim();
  }
  return `${secs}s`;
};

export { toTitleCase, truncateString, formatIntervalDisplay };
