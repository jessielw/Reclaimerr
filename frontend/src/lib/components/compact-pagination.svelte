<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import ChevronsLeft from "@lucide/svelte/icons/chevrons-left";
  import ChevronLeft from "@lucide/svelte/icons/chevron-left";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import ChevronsRight from "@lucide/svelte/icons/chevrons-right";

  interface Props {
    currentPage: number;
    totalPages: number;
    maxVisiblePages?: number;
    onPageChange?: (page: number) => void;
  }

  let {
    currentPage,
    totalPages,
    maxVisiblePages = 3,
    onPageChange = () => {},
  }: Props = $props();

  const pageButtons = $derived.by(() => {
    if (totalPages <= 0) return [];
    const visible = Math.max(1, maxVisiblePages);

    if (totalPages <= visible) {
      return Array.from({ length: totalPages }, (_, i) => i + 1);
    }

    const half = Math.floor(visible / 2);
    let start = currentPage - half;
    let end = start + visible - 1;

    if (start < 1) {
      start = 1;
      end = visible;
    }

    if (end > totalPages) {
      end = totalPages;
      start = Math.max(1, end - visible + 1);
    }

    return Array.from({ length: end - start + 1 }, (_, i) => start + i);
  });

  const goToPage = (page: number) => {
    if (page < 1 || page > totalPages || page === currentPage) return;
    onPageChange(page);
  };
</script>

<div class="flex items-center gap-1">
  <Button
    size="sm"
    class="h-8 w-8 p-0 cursor-pointer border border-gray-400 bg-button-1 hover:bg-button-1-hover 
      text-button-1-foreground shadow"
    disabled={currentPage <= 1}
    onclick={() => goToPage(1)}
    aria-label="First page"
  >
    <ChevronsLeft class="size-3.5" />
  </Button>

  <Button
    size="sm"
    class="h-8 w-8 p-0 cursor-pointer border border-gray-400 bg-button-1 hover:bg-button-1-hover 
      text-button-1-foreground shadow"
    disabled={currentPage <= 1}
    onclick={() => goToPage(currentPage - 1)}
    aria-label="Previous page"
  >
    <ChevronLeft class="size-3.5" />
  </Button>
  {#each pageButtons as page (page)}
    <Button
      size="sm"
      class="h-8 w-8 p-0 cursor-pointer border border-gray-400 shadow
        {page === currentPage
        ? 'bg-primary hover:bg-primary text-primary-foreground'
        : 'bg-button-1 hover:bg-button-1-hover text-button-1-foreground'}"
      onclick={() => goToPage(page)}
      aria-label={`Go to page ${page}`}
    >
      {page}
    </Button>
  {/each}

  <Button
    size="sm"
    class="h-8 w-8 p-0 cursor-pointer border border-gray-400 bg-button-1 hover:bg-button-1-hover 
      text-button-1-foreground shadow"
    disabled={currentPage >= totalPages}
    onclick={() => goToPage(currentPage + 1)}
    aria-label="Next page"
  >
    <ChevronRight class="size-3.5" />
  </Button>

  <Button
    size="sm"
    class="h-8 w-8 p-0 cursor-pointer border border-gray-400 bg-button-1 hover:bg-button-1-hover 
      text-button-1-foreground shadow"
    disabled={currentPage >= totalPages}
    onclick={() => goToPage(totalPages)}
    aria-label="Last page"
  >
    <ChevronsRight class="size-3.5" />
  </Button>
</div>
