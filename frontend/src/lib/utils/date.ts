function formatDate(dateString: string): string {
  return new Date(dateString).toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function formatDistanceToNow(dateString: string): string {
  const date = new Date(dateString);
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
}

export { formatDate, formatDistanceToNow };
