<script lang="ts">
  import InfoIcon from "@lucide/svelte/icons/info";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
  import Bug from "@lucide/svelte/icons/bug";

  const types: Record<
    "info" | "warning" | "error",
    {
      main: string;
      header: string;
      icon: {
        icon: typeof InfoIcon | typeof TriangleAlert | typeof Bug;
        class: string;
      };
      bg: string;
      border: string;
      text: string;
    }
  > = {
    info: {
      main: "bg-primary/10 border border-primary/70 text-foreground/75",
      header: "bg-primary/12",
      icon: {
        icon: InfoIcon,
        class: "text-foreground/75 size-4",
      },

      bg: "bg-primary/10",
      border: "border-primary/70",
      text: "text-foreground",
    },
    warning: {
      main: "bg-call-to-action/10 border border-call-to-action/70 text-foreground/75",
      header: "bg-call-to-action/12",
      icon: {
        icon: TriangleAlert,
        class: "text-foreground/75 size-4",
      },

      bg: "bg-call-to-action/10",
      border: "border-call-to-action/70",
      text: "text-foreground",
    },
    error: {
      main: "bg-warning/10 border border-warning/70 text-foreground/75",
      header: "bg-warning/12",
      icon: {
        icon: Bug,
        class: "text-foreground/75 size-4",
      },

      bg: "bg-warning/10",
      border: "border-warning/70",
      text: "text-foreground",
    },
  };

  interface Props {
    title?: string;
    type?: "info" | "warning" | "error";
    children?: any;
  }
  let { title = "Info", type = "info", children = undefined }: Props = $props();

  const IconComponent = $derived.by(() => {
    return types[type].icon.icon;
  });
</script>

<div
  class={`flex flex-col border ${types[type].main} overflow-hidden
  rounded-sm shadow`}
>
  <span class={`flex items-center gap-2 ${types[type].header} p-1 pl-2.5`}>
    <IconComponent class={types[type].icon.class} />
    <span class="text-sm font-semibold">{title}</span>
  </span>
  {#if children}
    <div class="text-sm p-3 text-foreground/85">
      {@render children?.()}
    </div>
  {/if}
</div>
