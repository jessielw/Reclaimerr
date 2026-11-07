import { writable } from "svelte/store";

export type ScheduledDeletionEditRequest = {
  scheduleId: number;
  action: "reschedule" | "cancel";
};

/** Candidate rows ask the scheduled-deletions panel to open its edit dialog. */
export const scheduledDeletionEditRequest =
  writable<ScheduledDeletionEditRequest | null>(null);
