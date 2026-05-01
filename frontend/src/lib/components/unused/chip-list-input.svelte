<script lang="ts">
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import X from "@lucide/svelte/icons/x";

  let {
    id,
    label,
    placeholder = "",
    values = null,
    onChange,
  }: {
    id: string;
    label: string;
    placeholder?: string;
    values: string[] | null;
    onChange: (next: string[] | null) => void;
  } = $props();

  let inputValue = $state("");

  const normalize = (value: string): string => value.trim().toLowerCase();

  const addValue = () => {
    const normalized = normalize(inputValue);
    if (!normalized) return;
    const existing = values ?? [];
    if (existing.includes(normalized)) {
      inputValue = "";
      return;
    }
    onChange([...existing, normalized]);
    inputValue = "";
  };

  const removeValue = (value: string) => {
    const next = (values ?? []).filter((item) => item !== value);
    onChange(next.length > 0 ? next : null);
  };
</script>

<div class="space-y-2">
  <Label for={id}>{label}</Label>
  <div class="flex gap-2">
    <Input
      {id}
      type="text"
      {placeholder}
      bind:value={inputValue}
      onkeydown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          addValue();
        }
      }}
    />
    <Button
      type="button"
      variant="secondary"
      onclick={addValue}
      class="cursor-pointer"
    >
      Add
    </Button>
  </div>

  {#if values && values.length > 0}
    <div class="flex flex-wrap gap-2">
      {#each values as value (value)}
        <div
          class="inline-flex items-center gap-1 rounded-md border border-border bg-muted/40 px-2 py-1 text-sm"
        >
          <span class="font-mono">{value}</span>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onclick={() => removeValue(value)}
            class="h-5 w-5 cursor-pointer"
          >
            <X class="size-3" />
          </Button>
        </div>
      {/each}
    </div>
  {/if}
</div>
