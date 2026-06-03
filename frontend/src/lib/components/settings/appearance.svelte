<script lang="ts">
  import Check from "@lucide/svelte/icons/check";
  import Palette from "@lucide/svelte/icons/palette";

  import { setThemeFamily, themeFamily } from "$lib/stores/theme-family";
  import { THEME_FAMILIES, type ThemeFamilyId } from "$lib/theme-families";

  const selectTheme = (id: ThemeFamilyId) => {
    if ($themeFamily === id) return;
    setThemeFamily(id);
  };

  const swatchLabels = ["Background", "Surface", "Primary", "Accent"] as const;
</script>

<section class="bg-card rounded-lg border border-border p-6">
  <div class="mb-4 flex items-start gap-3">
    <div
      class="flex size-10 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary"
    >
      <Palette class="size-5" />
    </div>
    <div class="min-w-0">
      <h2 class="text-xl font-semibold text-foreground">Appearance</h2>
      <p class="mt-1 text-sm text-muted-foreground">
        Pick a palette. The sidebar toggle still controls light and dark mode.
      </p>
    </div>
  </div>

  <div class="grid gap-3 lg:grid-cols-2 xl:grid-cols-4">
    {#each THEME_FAMILIES as preset}
      <button
        type="button"
        aria-pressed={$themeFamily === preset.id}
        onclick={() => selectTheme(preset.id)}
        class="group rounded-xl border p-4 text-left transition-all cursor-pointer outline-none focus-visible:ring-2
          focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background
          {$themeFamily === preset.id
          ? 'border-primary bg-primary/5 shadow-lg shadow-primary/10'
          : 'border-border bg-background/60 hover:border-primary/40 hover:bg-accent/30'}"
      >
        <div class="flex items-start justify-between gap-3">
          <div>
            <p class="font-medium text-foreground">{preset.label}</p>
            <p class="mt-1 text-xs leading-5 text-muted-foreground">
              {preset.description}
            </p>
          </div>
          {#if $themeFamily === preset.id}
            <span
              class="inline-flex size-7 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground"
            >
              <Check class="size-4" />
            </span>
          {/if}
        </div>

        <div class="mt-4 space-y-3">
          <div>
            <p
              class="text-[11px] font-semibold uppercase tracking-[0.2em] text-muted-foreground"
            >
              Light
            </p>
            <div class="mt-2 flex items-center gap-2">
              {#each preset.light as swatch, index}
                <span
                  class="size-4 rounded-full ring-1 ring-border shadow-sm"
                  style={`background-color: ${swatch};`}
                  aria-hidden="true"
                  title={swatchLabels[index]}
                ></span>
              {/each}
            </div>
          </div>

          <div>
            <p
              class="text-[11px] font-semibold uppercase tracking-[0.2em] text-muted-foreground"
            >
              Dark
            </p>
            <div class="mt-2 flex items-center gap-2">
              {#each preset.dark as swatch, index}
                <span
                  class="size-4 rounded-full ring-1 ring-border shadow-sm"
                  style={`background-color: ${swatch};`}
                  aria-hidden="true"
                  title={swatchLabels[index]}
                ></span>
              {/each}
            </div>
          </div>
        </div>
      </button>
    {/each}
  </div>
</section>
