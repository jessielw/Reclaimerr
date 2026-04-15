<script lang="ts">
  import { onMount } from "svelte";
  import { location } from "svelte-spa-router";
  import { auth } from "$lib/stores/auth";
  import { get_api } from "$lib/api";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
  import CircleAlert from "@lucide/svelte/icons/circle-alert";
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import { AlertLevel } from "$lib/types/shared";

  interface SystemAlert {
    id: string;
    alert_level: AlertLevel;
    title: string;
    message: string;
    action_label: string | null;
    action_href: string | null;
  }

  let alerts = $state<SystemAlert[]>([]);

  const fetchAlerts = async () => {
    if (!$auth.isAuthenticated) return;
    try {
      alerts = await get_api<SystemAlert[]>("/api/info/alerts");
    } catch {
      // fail silently (alerts are best effort)
    }
  };

  onMount(fetchAlerts);

  // fetch again whenever the route changes so alerts clear as soon as the user
  // fixes the issue
  $effect(() => {
    void $location;
    fetchAlerts();
  });

  const severityStyles: Record<
    AlertLevel,
    { banner: string; icon: string; IconComponent: typeof TriangleAlert }
  > = {
    warning: {
      banner: "bg-yield/10 border-yield/50 text-foreground/90",
      icon: "text-yield shrink-0 mt-0.5",
      IconComponent: TriangleAlert,
    },
    error: {
      banner: "bg-destructive/10 border-destructive/50 text-foreground/90",
      icon: "text-destructive shrink-0 mt-0.5",
      IconComponent: CircleAlert,
    },
  };
</script>

{#if alerts.length > 0}
  <div class="flex flex-col gap-0 border-b border-border">
    {#each alerts as alert (alert.id)}
      {@const style =
        severityStyles[alert.alert_level] ?? severityStyles.warning}
      <div
        class="flex items-start gap-3 px-4 py-3 border-b border-inherit last:border-b-0 {style.banner}"
      >
        <style.IconComponent class="size-4 {style.icon}" />
        <div class="flex-1 min-w-0">
          <p class="text-sm font-semibold leading-snug">{alert.title}</p>
          <p class="text-xs text-foreground/70 mt-0.5 leading-relaxed">
            {alert.message}
          </p>
        </div>
        {#if alert.action_label && alert.action_href}
          <a
            href={alert.action_href}
            class="shrink-0 inline-flex items-center gap-1 text-xs font-medium underline
              underline-offset-2 hover:opacity-70 transition-opacity mt-0.5"
          >
            {alert.action_label}
            <ExternalLink class="size-3" />
          </a>
        {/if}
      </div>
    {/each}
  </div>
{/if}
