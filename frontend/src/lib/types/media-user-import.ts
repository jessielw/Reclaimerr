import type { UserRole } from "./shared";

export interface MediaImportProvider {
  service_config_id: number;
  service_type: string;
  name: string;
}

export interface MediaImportAccount {
  id: number;
  username: string;
  display_name: string | null;
  email: string | null;
  role: UserRole;
  is_active: boolean;
}

export interface MediaUserImportRow {
  source_user_id: string;
  username: string;
  email: string | null;
  local_username: string | null;
  account: MediaImportAccount | null;
  status:
    | "ready_to_create"
    | "ready_to_link"
    | "needs_review"
    | "conflict"
    | "skipped"
    | "created"
    | "linked"
    | "error";
  message: string;
  revision: string;
}

export interface MediaUserImportResponse {
  rows: MediaUserImportRow[];
  ready: number;
  created: number;
  linked: number;
  skipped: number;
  errors: number;
}
