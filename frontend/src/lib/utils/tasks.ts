import { TaskStatus } from "$lib/types/shared";

// status text
const getTaskStatusText = (status: TaskStatus | null): string => {
  if (!status) return "Unknown";
  if (status === TaskStatus.Queued) return "Queued";
  return status.charAt(0).toUpperCase() + status.slice(1);
};

// returns a Tailwind CSS class for the background color corresponding to a given task status
const getStatusColor = (status: TaskStatus): string => {
  switch (status) {
    case TaskStatus.Queued:
      return "bg-amber-500";
    case TaskStatus.Completed:
      return "bg-green-500";
    case TaskStatus.Error:
      return "bg-red-500";
    case TaskStatus.Running:
      return "bg-blue-500";
    case TaskStatus.Disabled:
      return "bg-gray-500";
    case TaskStatus.Scheduled:
      return "bg-yellow-500";
    default:
      return "bg-gray-500";
  }
};

export { getStatusColor, getTaskStatusText };
