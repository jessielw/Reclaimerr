<script lang="ts">
  import { get_api, post_api } from "$lib/api";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import * as Dialog from "$lib/components/ui/dialog/index.js";
  import { MediaType } from "$lib/types/shared";
  import ArrowLeft from "@lucide/svelte/icons/arrow-left";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import FolderIcon from "@lucide/svelte/icons/folder";
  import Plus from "@lucide/svelte/icons/plus";

  interface PathNode {
    path: string;
    name: string;
    children: PathNode[];
  }

  interface Props {
    open?: boolean;
    mediaType: MediaType;
    libraryIds?: string[] | null;
    onSelect: (pattern: string) => void;
  }

  let {
    open = $bindable(false),
    mediaType,
    libraryIds = null,
    onSelect,
  }: Props = $props();

  let pathTree = $state<PathNode[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let currentPathSelection = $state<string>("");
  let pathSuffixInput = $state<string>("");
  let pathSuffixError = $state<string | null>(null);
  let validating = $state(false);
  let validatedPattern = $state<string | null>(null);
  let loadedScopeKey = $state<string | null>(null);

  const scopeKey = () =>
    `${mediaType}|${(libraryIds ?? []).slice().sort().join(",")}`;

  const loadPathTree = async () => {
    const key = scopeKey();
    if (loadedScopeKey === key && pathTree.length > 0) return;

    loading = true;
    error = null;
    try {
      const params = new URLSearchParams();
      params.set("media_type", mediaType);
      for (const id of libraryIds ?? []) params.append("library_ids", id);
      pathTree = await get_api<PathNode[]>(`/api/rules/path-tree?${params}`);
      currentPathSelection = "";
      pathSuffixInput = "";
      pathSuffixError = null;
      validatedPattern = null;
      loadedScopeKey = key;
    } catch (e: any) {
      error = e.message ?? "Failed to load path tree";
      pathTree = [];
    } finally {
      loading = false;
    }
  };

  // find node in tree matching the given path, or null if not found
  const findNode = (nodes: PathNode[], target: string): PathNode | null => {
    for (const node of nodes) {
      if (node.path === target) return node;
      if (target.startsWith(node.path + "/")) {
        const found = findNode(node.children, target);
        if (found) return found;
      }
    }
    return null;
  };

  // derive the children of the currently selected path for display in the folder list
  const currentChildren = $derived.by<PathNode[]>(() => {
    if (!currentPathSelection) return pathTree;
    const node = findNode(pathTree, currentPathSelection);
    return node ? node.children : [];
  });

  // derive the breadcrumb trail for the currently selected path
  const breadcrumb = $derived.by<{ label: string; path: string }[]>(() => {
    if (!currentPathSelection) return [];
    const crumbs: { label: string; path: string }[] = [];
    let nodes = pathTree;
    let remaining = currentPathSelection;
    while (remaining) {
      const match = nodes.find(
        (n) => n.path === remaining || remaining.startsWith(n.path + "/"),
      );
      if (!match) break;
      crumbs.push({ label: match.name, path: match.path });
      if (match.path === remaining) break;
      nodes = match.children;
    }
    return crumbs;
  });

  // navigation helpers
  const navigateInto = (node: PathNode) => {
    currentPathSelection = node.path;
  };

  const navigateUp = () => {
    if (!currentPathSelection) return;
    const idx = currentPathSelection.lastIndexOf("/");
    currentPathSelection = idx <= 0 ? "" : currentPathSelection.slice(0, idx);
  };

  // helper to join the base path and suffix for display in the preview box,
  // ensuring there's exactly one separator between them
  const joinPathAndSuffix = (base: string, suffix: string): string => {
    const trimmedBase = base.replace(/[\\/]+$/, "");
    const trimmedSuffix = suffix.trim().replace(/^[\\/]+/, "");
    if (!trimmedSuffix) return trimmedBase;
    const sep =
      trimmedBase.includes("\\") && !trimmedBase.includes("/") ? "\\" : "/";
    return `${trimmedBase}${sep}${trimmedSuffix}`;
  };

  // validate the combined pattern on the backend and return the result
  const validateRegexOnBackend = async (
    basePath: string,
    suffix: string,
  ): Promise<{
    valid: boolean;
    error: string | null;
    pattern: string | null;
  }> => {
    try {
      return await post_api<{
        valid: boolean;
        error: string | null;
        pattern: string | null;
      }>("/api/rules/validate-regex", { base_path: basePath, suffix });
    } catch (e: any) {
      return {
        valid: false,
        error: e.message ?? "Validation failed",
        pattern: null,
      };
    }
  };

  // test the current path selection and suffix input, updating the validated
  // pattern or error message accordingly
  const testPattern = async () => {
    if (!currentPathSelection) {
      validatedPattern = null;
      pathSuffixError = null;
      return;
    }
    validating = true;
    const validation = await validateRegexOnBackend(
      currentPathSelection,
      pathSuffixInput ?? "",
    );
    validating = false;
    if (!validation.valid) {
      validatedPattern = null;
      pathSuffixError = validation.error ?? "Invalid regex pattern";
      return;
    }
    validatedPattern = validation.pattern;
    pathSuffixError = null;
  };

  // handler for the "Add Pattern" button (validate the pattern and if valid,
  // pass it to the parent and close the dialog)
  const addPattern = async () => {
    await testPattern();
    if (!validatedPattern) return;
    onSelect(validatedPattern);
    open = false;
  };

  // derive the display pattern for the preview box so it's always non empty
  const previewPattern = $derived(
    validatedPattern ||
      joinPathAndSuffix(currentPathSelection, pathSuffixInput) ||
      "-",
  );

  $effect(() => {
    if (!open) return;
    void mediaType;
    void libraryIds;
    void loadPathTree();
  });

  $effect(() => {
    if (!open) return;
    void currentPathSelection;
    void pathSuffixInput;
    validatedPattern = null;
    pathSuffixError = null;
  });
</script>

<Dialog.Root bind:open>
  <Dialog.Content
    class="
      bg-card border border-border rounded-xl p-0
      min-w-[min(80vw,64rem)] sm:min-w-[min(80vw,64rem)]
      h-[min(95vh,42rem)]
      max-w-none
      flex flex-col
      overflow-hidden
      text-foreground
    "
  >
    <!-- header - fixed height -->
    <Dialog.Header class="px-6 pt-5 pb-3 shrink-0 border-b border-border">
      <Dialog.Title class="text-base font-semibold"
        >Select Path Pattern</Dialog.Title
      >
      <Dialog.Description class="text-sm text-muted-foreground mt-0.5">
        Browse indexed directories, optionally add a regex suffix, then insert
        the resulting pattern.
      </Dialog.Description>
    </Dialog.Header>

    <!-- scrollable body -->
    <div class="flex-1 overflow-y-auto px-6 py-4 space-y-4 min-h-0">
      {#if loading}
        <p class="text-sm text-muted-foreground">Loading paths…</p>
      {:else if error}
        <p class="text-sm text-destructive">{error}</p>
      {:else if pathTree.length === 0}
        <p class="text-sm text-muted-foreground">
          No indexed media paths are available for this scope.
        </p>
      {:else}
        <!-- breadcrumb nav -->
        <nav
          aria-label="Path breadcrumb"
          class="flex items-center gap-0.5 flex-wrap text-sm"
        >
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onclick={() => (currentPathSelection = "")}
            class="cursor-pointer h-7 px-2 gap-1 text-xs font-medium shrink-0"
            disabled={!currentPathSelection}
          >
            <FolderIcon class="size-3.5" />
            Roots
          </Button>

          {#each breadcrumb as crumb, i}
            <ChevronRight class="size-3 text-muted-foreground/60 shrink-0" />
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onclick={() => (currentPathSelection = crumb.path)}
              disabled={i === breadcrumb.length - 1}
              class="cursor-pointer h-7 px-2 text-xs font-mono min-h-4 truncate shrink-0"
              title={crumb.path}
            >
              {crumb.label}
            </Button>
          {/each}

          {#if currentPathSelection}
            <div class="ml-auto shrink-0">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onclick={navigateUp}
                class="cursor-pointer h-7 px-2 gap-1 text-xs"
              >
                <ArrowLeft class="size-3.5" />
                Up
              </Button>
            </div>
          {/if}
        </nav>

        <!-- folder list - fixed height so dialog doesn't jump -->
        <div
          class="rounded-lg border border-border divide-y divide-border h-48 overflow-y-auto"
        >
          {#if currentChildren.length === 0}
            <div
              class="flex items-center h-full px-4 text-sm text-muted-foreground"
            >
              {#if currentPathSelection}
                No deeper folders - add an optional regex suffix below.
              {:else}
                No folders available.
              {/if}
            </div>
          {:else}
            {#each currentChildren as node}
              <div
                class="flex items-center justify-between gap-2 px-3 py-2 hover:bg-muted/50 cursor-pointer transition-colors"
                role="button"
                tabindex="0"
                onclick={() => navigateInto(node)}
                onkeydown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    navigateInto(node);
                  }
                }}
              >
                <div class="flex items-center gap-2 min-w-0">
                  <FolderIcon class="size-4 shrink-0 text-muted-foreground" />
                  <span class="font-mono text-sm truncate">{node.name}</span>
                </div>
                {#if node.children.length > 0}
                  <ChevronRight
                    class="size-4 text-muted-foreground/60 shrink-0"
                  />
                {/if}
              </div>
            {/each}
          {/if}
        </div>

        <!-- regex suffix input -->
        <div>
          <Label for="path-suffix" class="text-sm"
            >Regex Suffix <span class="text-muted-foreground font-normal"
              >(optional)</span
            ></Label
          >
          <Input
            id="path-suffix"
            type="text"
            placeholder="e.g. .*1080p.* or .*/Extras/.*"
            bind:value={pathSuffixInput}
            disabled={!currentPathSelection}
            class="font-mono text-sm {pathSuffixError
              ? 'border-destructive focus-visible:ring-destructive'
              : ''}"
          />
          <!-- reserve vertical space so the layout doesn't jump on error -->
          <p
            class="text-xs min-h-4 {pathSuffixError
              ? 'text-destructive'
              : 'invisible'}"
          >
            {pathSuffixError ?? "no error"}
          </p>
        </div>

        <!-- pattern preview - fixed min height so it never collapses -->
        <div>
          <Label class="text-sm">Pattern Preview</Label>
          <div
            class="
              rounded-md border border-input bg-muted
              px-3 py-2
              text-sm font-mono break-all
              min-h-10
              flex items-center
              {!currentPathSelection ? 'text-muted-foreground/50 italic' : ''}
            "
          >
            {previewPattern}
          </div>
        </div>
      {/if}
    </div>

    <!-- footer - fixed height, always visible -->
    <div
      class="shrink-0 border-t border-border px-6 py-3 flex items-center justify-end gap-2"
    >
      <Button
        type="button"
        variant="secondary"
        class="cursor-pointer"
        onclick={() => (open = false)}
      >
        Cancel
      </Button>
      <Button
        type="button"
        class="cursor-pointer gap-1.5"
        onclick={addPattern}
        disabled={!currentPathSelection || !!pathSuffixError || validating}
      >
        <Plus class="size-4" />
        {validating ? "Validating…" : "Add Pattern"}
      </Button>
    </div>
  </Dialog.Content>
</Dialog.Root>
