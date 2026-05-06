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
 * Utility function to format file sizes in bytes to a human readable string with appropriate
 * units (B, KB, MB, GB, TB).
 * If the input is null, it returns "Unknown". If the input is zero, it returns "0 B".
 * @param bytes The file size in bytes.
 * @param decimals The number of decimal places to include in the formatted string.
 * @returns A formatted string representing the file size with appropriate units.
 */
const formatFileSize = (bytes: number | null, decimals = 2): string => {
  if (bytes == null) return "Unknown";
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(
    Math.floor(Math.log(bytes) / Math.log(k)),
    sizes.length - 1,
  );
  const value = bytes / Math.pow(k, i);
  return `${value.toFixed(decimals)} ${sizes[i]}`;
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

/**
 * Utility function to clean a resolution string
 * @param res The resolution string to clean (e.g. "1080" -> "1080p")
 * @returns The cleaned resolution string, original string upper-cased, or null
 */
const cleanResolutionString = (res: string | null): string | null => {
  if (!res) return null;
  if (/^[0-9]+$/.test(res)) {
    return res + "p";
  }
  return res.toUpperCase();
};

export { formatSizeToGB, formatRuntime, formatFileSize, cleanResolutionString };
