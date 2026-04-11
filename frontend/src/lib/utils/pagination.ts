/**
 * Per page options and localStorage persistence for paginated views.
 *
 * Usage:
 *   const { perPage, setPerPage, PER_PAGE_OPTIONS } = createPerPageState("my_view_per_page");
 */
export const PER_PAGE_OPTIONS = [10, 25, 50, 100];
const PER_PAGE_MIN = 10;
const PER_PAGE_MAX = 100;
const PER_PAGE_DEFAULT = 25;

export const createPerPageState = (
  storageKey: string,
): {
  getInitial: () => number;
  save: (value: number) => void;
} => {
  return {
    getInitial(): number {
      const stored = parseInt(localStorage.getItem(storageKey) ?? "", 10);
      if (!isNaN(stored) && stored >= PER_PAGE_MIN && stored <= PER_PAGE_MAX)
        return stored;
      return PER_PAGE_DEFAULT;
    },
    save(value: number): void {
      localStorage.setItem(storageKey, value.toString());
    },
  };
};

/**
 * Persistent state for generic filter values (strings, booleans, etc.).
 *
 * Usage:
 *   const _sortByStore = createFilterState("my_view_sort_by", "title");
 *   let sortBy = $state(_sortByStore.getInitial());
 *   $effect(() => _sortByStore.save(sortBy));
 */
export const createFilterState = <T>(
  storageKey: string,
  defaultValue: T,
): { getInitial: () => T; save: (value: T) => void } => {
  return {
    getInitial(): T {
      try {
        const stored = localStorage.getItem(storageKey);
        if (stored === null) return defaultValue;
        return JSON.parse(stored) as T;
      } catch {
        return defaultValue;
      }
    },
    save(value: T): void {
      localStorage.setItem(storageKey, JSON.stringify(value));
    },
  };
};
