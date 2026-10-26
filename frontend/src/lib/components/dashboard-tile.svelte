<script lang="ts">
  import type { Snippet } from "svelte";

  interface Props {
    /** Page behind the number; the tile renders as a link only when set. */
    href?: string;
    class?: string;
    children: Snippet;
  }
  let { href, class: className = "", children }: Props = $props();
</script>

<svelte:element
  this={href ? "a" : "article"}
  {href}
  class={[
    "block bg-card rounded-lg border border-border p-5",
    href && "hover:border-primary/50 transition-colors",
    className,
  ]}
>
  {@render children()}
</svelte:element>
