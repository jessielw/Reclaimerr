import { auth } from "./stores/auth";

/**
 * make an authenticated API request
 */
export async function fetchAPI(url: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);

  // only set Content-Type for JSON requests, not for FormData (browser sets it automatically)
  if (options.body && typeof options.body === "string") {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(url, {
    ...options,
    headers,
    credentials: "include",
  });

  // handle 401 Unauthorized - token expired or invalid
  if (response.status === 401) {
    auth.logout();
    throw new Error("Session expired. Please login again.");
  }

  return response;
}

/**
 * helper for GET requests
 */
export async function get_api<T>(
  url: string,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetchAPI(url, { signal });

  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: "Request failed" }));
    throw new Error(
      error.detail || `Request failed with status ${response.status}`,
    );
  }

  return response.json();
}

/**
 * helper for POST requests
 */
export async function post_api<T>(url: string, data: any): Promise<T> {
  // Check if data is FormData (for file uploads)
  const isFormData = data instanceof FormData;

  const response = await fetchAPI(url, {
    method: "POST",
    body: isFormData ? data : JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: "Request failed" }));
    throw new Error(
      error.detail || `Request failed with status ${response.status}`,
    );
  }

  return response.json();
}

/**
 * helper for PUT requests
 */
export async function put_api<T>(url: string, data: any): Promise<T> {
  const isFormData = data instanceof FormData;

  const response = await fetchAPI(url, {
    method: "PUT",
    body: isFormData ? data : JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: "Request failed" }));
    throw new Error(
      error.detail || `Request failed with status ${response.status}`,
    );
  }

  return response.json();
}

/**
 * helper for DELETE requests
 */
export async function delete_api<T>(url: string): Promise<T> {
  const response = await fetchAPI(url, {
    method: "DELETE",
  });

  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: "Request failed" }));
    throw new Error(
      error.detail || `Request failed with status ${response.status}`,
    );
  }

  return response.json();
}
