/**
 * Utility function to format file sizes in bytes to a human-readable string in GB.
 * If the input is null or zero, it returns "Unknown".
 * @param bytes The file size in bytes.
 * @returns A formatted string representing the file size in GB.
 */
const formatSizeToGB = (bytes: number | null): string => {
  if (!bytes) return "Unknown";
  const gb = bytes / (1024 * 1024 * 1024);
  return `${gb.toFixed(2)} GB`;
};

/**
 * Utility function to format runtime in minutes to a human-readable string in hours and minutes.
 * If the input is null or zero, it returns "Unknown".
 * @param minutes The runtime in minutes.
 * @returns A formatted string representing the runtime in hours and minutes.
 */
const formatRuntime = (minutes: number | null): string => {
  if (!minutes) return "Unknown";
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return `${hours}h ${mins}m`;
};

export { formatSizeToGB, formatRuntime };
