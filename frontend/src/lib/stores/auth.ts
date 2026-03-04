import { writable } from "svelte/store";
import type { UserProfile } from "$lib/types/shared";
import type { AuthState } from "$lib/types/auth";

function createAuthStore() {
  const { subscribe, set, update } = writable<AuthState>({
    isAuthenticated: false,
    user: null,
    loading: true, // start with loading=true while we check for existing session
  });

  // initialize auth state by checking if the cookie session is still valid
  async function init() {
    try {
      // verify session is still valid by fetching user info (cookie sent automatically)
      const response = await fetch("/api/account/me", {
        credentials: "include",
      });

      if (response.ok) {
        const user = await response.json();
        set({
          isAuthenticated: true,
          user,
          loading: false,
        });
      } else {
        set({
          isAuthenticated: false,
          user: null,
          loading: false,
        });
      }
    } catch (error) {
      console.error("Failed to verify session:", error);
      set({
        isAuthenticated: false,
        user: null,
        loading: false,
      });
    }
  }

  // Re-verify the session when the browser tab becomes visible again.
  // This handles the case where the JWT expired while the user was away —
  // instead of getting a jarring logout mid-interaction, we check proactively.
  if (typeof document !== "undefined") {
    let lastCheck = 0;
    const RECHECK_INTERVAL_MS = 5 * 60 * 1000; // at most once per 5 minutes

    document.addEventListener("visibilitychange", () => {
      if (
        document.visibilityState === "visible" &&
        Date.now() - lastCheck > RECHECK_INTERVAL_MS
      ) {
        lastCheck = Date.now();
        init();
      }
    });
  }

  return {
    subscribe,
    init,
    // login
    login: async (username: string, password: string) => {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ username, password }),
        credentials: "include",
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "Login failed");
      }

      const data = await response.json();

      set({
        isAuthenticated: true,
        user: data.user,
        loading: false,
      });

      return data;
    },

    // logout
    logout: async () => {
      try {
        await fetch("/api/auth/logout", {
          method: "POST",
          credentials: "include",
        });
      } catch {
        // ignore network errors on logout
      }
      set({
        isAuthenticated: false,
        user: null,
        loading: false,
      });
    },

    // update user info (e.g., after profile update)
    updateUser: (user: UserProfile) => {
      update((state) => ({
        ...state,
        user,
      }));
    },
  };
}

export const auth = createAuthStore();
