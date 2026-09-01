import type { MEDIA_SERVERS } from "$lib/types/shared";

export type ServerKey = (typeof MEDIA_SERVERS)[number];

export type MediaServerConfig = {
  id: number | null;
  name: string;
  enabled: boolean;
  baseUrl: string;
  isMain: boolean;
};

// One saved media server row. Drafts never live here - a server only joins the
// list once the API has accepted it and handed back an id, so every entry in
// the list is addressable by `config.id`.
export type MediaServerState = {
  serverKey: ServerKey;
  config: MediaServerConfig;
  apiKeyIsSet: boolean;
  syncing: boolean;
  lastSyncedAt: string | null;
};

export type SaveServiceResponse = {
  message: string;
  sync_action: "resync" | "sync" | null;
  data: {
    id: number;
    name: string;
    service_type: string;
    enabled: boolean;
    base_url: string;
    is_main: boolean;
  };
};
