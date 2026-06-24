import { PageAccess, UserRole, type User } from "$lib/types/shared";

export type PageAccessOption = {
  value: PageAccess;
  label: string;
  path: string;
  description: string;
};

export const PAGE_ACCESS_OPTIONS: PageAccessOption[] = [
  {
    value: PageAccess.Dashboard,
    label: "Dashboard",
    path: "/",
    description: "View the app overview and summary metrics.",
  },
  {
    value: PageAccess.Movies,
    label: "Movies",
    path: "/movies",
    description: "Browse synced movie media.",
  },
  {
    value: PageAccess.Series,
    label: "Series",
    path: "/series",
    description: "Browse synced series media.",
  },
  {
    value: PageAccess.Requests,
    label: "Requests",
    path: "/requests",
    description: "View request queues.",
  },
  {
    value: PageAccess.Protected,
    label: "Protected",
    path: "/protected",
    description: "View protected media entries.",
  },
  {
    value: PageAccess.Candidates,
    label: "Candidates",
    path: "/candidates",
    description: "Review cleanup candidates.",
  },
  {
    value: PageAccess.History,
    label: "History",
    path: "/history",
    description: "Browse reclaim history.",
  },
  {
    value: PageAccess.Settings,
    label: "Settings",
    path: "/settings",
    description: "Open settings tabs available to this user.",
  },
];

export const DEFAULT_NEW_USER_ALLOWED_PAGES = [
  PageAccess.Candidates,
  PageAccess.Settings,
];

const pageByPath = new Map(
  PAGE_ACCESS_OPTIONS.map((item) => [item.path, item]),
);
const redirectPreference = [
  PageAccess.Candidates,
  PageAccess.Dashboard,
  PageAccess.Movies,
  PageAccess.Series,
  PageAccess.Requests,
  PageAccess.Protected,
  PageAccess.History,
  PageAccess.Settings,
];

export const pageForPath = (path: string): PageAccess | null => {
  return pageByPath.get(path)?.value ?? null;
};

export const hasPageAccess = (
  user: Pick<User, "role" | "allowed_pages"> | null | undefined,
  page: PageAccess,
): boolean => {
  if (!user) return false;
  if (user.role === UserRole.Admin) return true;
  if (user.allowed_pages === null) return true;
  return user.allowed_pages.includes(page);
};

export const firstAccessiblePath = (
  user: Pick<User, "role" | "allowed_pages"> | null | undefined,
): string => {
  if (!user || user.role === UserRole.Admin || user.allowed_pages === null) {
    return "/";
  }

  for (const page of redirectPreference) {
    if (!user.allowed_pages.includes(page)) continue;
    return (
      PAGE_ACCESS_OPTIONS.find((option) => option.value === page)?.path ?? "/"
    );
  }

  return "/candidates";
};
