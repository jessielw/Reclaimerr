<script lang="ts">
  export let error: string = "";
</script>

{#if error}
  <div
    class="mb-6 p-4 bg-red-900/50 border border-red-700 rounded-lg text-red-200"
  >
    {error}
  </div>
{/if}
