<script lang="ts">
  import { auth } from "$lib/stores/auth";
  import logoImage from "$lib/assets/logo.png";

  let username = "";
  let password = "";
  let error = "";
  let loading = false;

  async function handleLogin() {
    error = "";
    loading = true;

    try {
      await auth.login(username, password);
      // successfully logged in, auth store will update and App.svelte will show dashboard
    } catch (err: any) {
      error = err.message || "Login failed";
    } finally {
      loading = false;
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === "Enter") {
      handleLogin();
    }
  }
</script>

<div
  class="min-h-screen flex items-center justify-center bg-gray-950 bg-linear-to-t from-gray-700 to-gray-900 px-4"
>
  <div class="max-w-md w-full space-y-8">
    <!-- logo/title -->
    <div class="text-center">
      <img src={logoImage} alt="Reclaimerr" class="w-32 h-32 mx-auto mb-4" />
      <h1 class="text-4xl font-bold text-blue-500 mb-2">Reclaimerr</h1>
    </div>

    <!-- login form -->
    <div class="bg-gray-900 rounded-lg shadow-xl p-8 border border-gray-800">
      {#if error}
        <div
          class="mb-4 p-3 bg-red-900/50 border border-red-700 rounded text-red-200 text-sm"
        >
          {error}
        </div>
      {/if}

      <form on:submit|preventDefault={handleLogin} class="space-y-4">
        <div>
          <input
            id="username"
            type="text"
            bind:value={username}
            on:keydown={handleKeydown}
            disabled={loading}
            class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-md text-white
            placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500
            focus:border-transparent disabled:opacity-50"
            placeholder="Username / Email"
            required
            autocomplete="username"
          />
        </div>

        <div>
          <input
            id="password"
            type="password"
            bind:value={password}
            on:keydown={handleKeydown}
            disabled={loading}
            class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-md text-white
            placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500
            focus:border-transparent disabled:opacity-50"
            placeholder="Password"
            required
            autocomplete="current-password"
          />
        </div>

        <button
          type="submit"
          disabled={loading}
          class="flex justify-center w-full py-2 px-4 bg-blue-600 hover:bg-blue-700
          disabled:bg-blue-800 disabled:cursor-not-allowed text-white font-medium rounded-md
          transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2
          focus:ring-offset-gray-900"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
            stroke-width="1.5"
            stroke="currentColor"
            class="size-6"
          >
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              d="M8.25 9V5.25A2.25 2.25 0 0 1 10.5 3h6a2.25 2.25 0 0 1 2.25 2.25v13.5A2.25 2.25 0 0 
              1 16.5 21h-6a2.25 2.25 0 0 1-2.25-2.25V15M12 9l3 3m0 0-3 3m3-3H2.25"
            />
          </svg>
          {loading ? "Signing in..." : "Sign In"}
        </button>
      </form>
    </div>
  </div>
</div>
