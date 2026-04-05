<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { get_api } from "$lib/api";
  import ErrorBox from "$lib/components/error-box.svelte";
  import Notice from "$lib/components/notice.svelte";
  import { formatDate, formatDistanceToNow } from "$lib/utils/date";
  import TrendingUp from "@lucide/svelte/icons/trending-up";
  import TrendingDown from "@lucide/svelte/icons/trending-down";
  import Minus from "@lucide/svelte/icons/minus";
  import type {
    DashboardActivityItem,
    DashboardResponse,
  } from "$lib/types/shared";

  // state
  let dashboard = $state<DashboardResponse | null>(null);
  let loading = $state(true);
  let error = $state("");
  let lastUpdatedAt = $state<string | null>(null);
  let refreshTimer: ReturnType<typeof setInterval> | null = null;
  let clockTimer: ReturnType<typeof setInterval> | null = null;
  let nowTick = $state(Date.now());
  let isFetching = false;

  // derived state
  const activity = $derived(dashboard?.activity ?? []);
  const isAdmin = $derived(dashboard?.viewer.can_view_admin_panels ?? false);
  const lastUpdatedLabel = $derived.by(() => {
    nowTick;
    if (!lastUpdatedAt) return null;
    return formatDistanceToNow(lastUpdatedAt);
  });
  const libraryTotal = $derived(
    (dashboard?.kpis.total_movies ?? 0) + (dashboard?.kpis.total_series ?? 0),
  );
  const librarySizeTotal = $derived(
    (dashboard?.kpis.total_movies_size_gb ?? 0) +
      (dashboard?.kpis.total_series_size_gb ?? 0),
  );
  const showSyncNotice = $derived(
    (dashboard?.media_server_configured ?? false) && libraryTotal === 0,
  );
  const requestBalance7d = $derived(
    (dashboard?.requests.approved_7d ?? 0) -
      (dashboard?.requests.denied_7d ?? 0),
  );
  const reclaimableMovieShare = $derived.by(() => {
    const total = dashboard?.kpis.reclaimable_total_gb ?? 0;
    if (!total) return 0;
    return Math.round(
      ((dashboard?.kpis.reclaimable_movies_gb ?? 0) / total) * 100,
    );
  });
  const reclaimableSeriesShare = $derived.by(() => {
    const total = dashboard?.kpis.reclaimable_total_gb ?? 0;
    if (!total) return 0;
    return Math.round(
      ((dashboard?.kpis.reclaimable_series_gb ?? 0) / total) * 100,
    );
  });

  // helpers
  type SizeUnit = "MB" | "GB" | "TB";
  const UNIT_CYCLE: SizeUnit[] = ["MB", "GB", "TB"];
  const SIZE_UNIT_KEY = "reclaimerr_dashboard_size_unit";
  const storedUnit = localStorage.getItem(SIZE_UNIT_KEY);
  let sizeUnit = $state<SizeUnit>(
    UNIT_CYCLE.includes(storedUnit as SizeUnit)
      ? (storedUnit as SizeUnit)
      : "GB",
  );
  $effect(() => {
    localStorage.setItem(SIZE_UNIT_KEY, sizeUnit);
  });
  const cycleSizeUnit = () => {
    const idx = UNIT_CYCLE.indexOf(sizeUnit);
    sizeUnit = UNIT_CYCLE[(idx + 1) % UNIT_CYCLE.length];
  };
  const formatSize = (gbValue: number) => {
    if (sizeUnit === "MB") return `${(gbValue * 1024).toFixed(0)} MB`;
    if (sizeUnit === "TB") return `${(gbValue / 1024).toFixed(2)} TB`;
    return `${gbValue.toFixed(2)} GB`;
  };
  const formatPercent = (value: number) => `${Math.round(value)}%`;
  const getTrendClass = (value: number) => {
    if (value > 0) return "text-green-500";
    if (value < 0) return "text-destructive";
    return "text-muted-foreground";
  };
  const formatSigned = (value: number) =>
    value > 0 ? `+${value}` : `${value}`;

  // activity helpers
  const getActivityLabel = (item: DashboardActivityItem) => {
    if (item.type === "request") {
      const title = item.title.toLowerCase();
      if (title.includes("approved")) return "Approved";
      if (title.includes("denied")) return "Denied";
      if (title.includes("pending")) return "Pending";
      return "Request";
    }
    if (item.type === "protected") return "Protected";
    if (item.type === "task") return "Task";
    return "Activity";
  };

  const getActivityBadgeClass = (item: DashboardActivityItem) => {
    const label = getActivityLabel(item);
    if (label === "Approved") return "bg-green-500/20 text-green-500";
    if (label === "Denied") return "bg-destructive/20 text-destructive";
    if (label === "Pending") return "bg-yellow-500/20 text-yellow-500";
    if (label === "Protected") return "bg-primary/20 text-primary";
    return "bg-muted text-muted-foreground";
  };

  const getActivityTitle = (item: DashboardActivityItem) => {
    if (item.type === "request") {
      const label = getActivityLabel(item);
      if (label === "Approved") return "Exception approved";
      if (label === "Denied") return "Exception denied";
      if (label === "Pending") return "New protection request";
      return "Protection request";
    }
    if (item.type === "protected") return "Media protected";
    return item.title;
  };

  const getActivitySubtitle = (item: DashboardActivityItem) => {
    const details: string[] = [];
    if (item.subtitle) details.push(item.subtitle);
    if (item.actor_display) details.push(`by ${item.actor_display}`);
    return details.join(" • ");
  };

  // determine if we should show last sync info for a service
  // (only for Plex and Jellyfin for now since those are the only ones with sync functionality)
  const shouldShowServiceSync = (serviceName: string) =>
    serviceName === "plex" || serviceName === "jellyfin";

  // fetch dashboard stats from API
  const fetchStats = async (showLoading = true) => {
    if (isFetching) return;

    try {
      isFetching = true;
      if (showLoading) {
        loading = true;
      }
      dashboard = await get_api<DashboardResponse>("/api/dashboard");
      lastUpdatedAt = new Date().toISOString();
      error = "";
    } catch (err: any) {
      console.error("Error fetching dashboard stats:", err);
      error = err.message;
    } finally {
      if (showLoading) {
        loading = false;
      }
      isFetching = false;
    }
  };

  // fetch stats when component mounts
  onMount(async () => {
    await fetchStats();
    refreshTimer = setInterval(() => {
      fetchStats(false);
    }, 60000);
    clockTimer = setInterval(() => {
      nowTick = Date.now();
    }, 1000);
  });

  onDestroy(() => {
    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }
    if (clockTimer) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  });
</script>

<div class="p-2.5 md:p-8">
  <div class="max-w-7xl mx-auto">
    <div class="mb-8">
      <h1 class="text-3xl font-bold text-foreground mb-2">Dashboard</h1>
      <p class="text-muted-foreground">
        Overview of your media library cleanup status
      </p>
      <div class="mt-2 flex items-center gap-3">
        {#if lastUpdatedLabel}
          <p class="text-xs text-muted-foreground">
            Last updated {lastUpdatedLabel}
          </p>
        {/if}
        <button
          onclick={cycleSizeUnit}
          class="text-xs px-2 py-0.5 rounded-full border border-border bg-secondary/50 text-muted-foreground
            hover:bg-secondary transition-colors cursor-pointer"
        >
          {sizeUnit}
        </button>
      </div>
    </div>

    {#if loading}
      <div
        class="bg-card rounded-lg border border-border p-8 text-center text-muted-foreground"
      >
        <div
          class="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-primary
            border-r-transparent"
        ></div>
        <p class="mt-4">Loading dashboard...</p>
      </div>
    {:else}
      <!-- show error box if there's an error -->
      <ErrorBox {error} />

      {#if dashboard}
        <div class="space-y-6 overflow-x-hidden">
          {#if showSyncNotice}
            <Notice type="warning" title="No Media Found">
              A media server is configured but no media has been synced yet. Run <strong
                >Sync Media</strong
              >
              in <strong>Tasks</strong> to populate your library.
            </Notice>
          {/if}
          <section class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
            <!-- movies -->
            <article
              class="bg-card rounded-lg border border-border p-5 min-h-28"
            >
              <p class="text-sm text-muted-foreground">Total Movies</p>
              <p class="text-3xl font-bold text-foreground mt-2">
                {dashboard.kpis.total_movies}
              </p>
              <p class="text-xs text-muted-foreground mt-2">
                {librarySizeTotal > 0
                  ? `${formatPercent((dashboard.kpis.total_movies_size_gb / librarySizeTotal) * 100)} of ` +
                    "library by size"
                  : "No library items yet"}
              </p>
            </article>

            <!-- series -->
            <article
              class="bg-card rounded-lg border border-border p-5 min-h-28"
            >
              <p class="text-sm text-muted-foreground">Total Series</p>
              <p class="text-3xl font-bold text-foreground mt-2">
                {dashboard.kpis.total_series}
              </p>
              <p class="text-xs text-muted-foreground mt-2">
                {librarySizeTotal > 0
                  ? `${formatPercent((dashboard.kpis.total_series_size_gb / librarySizeTotal) * 100)} of ` +
                    "library by size"
                  : "No library items yet"}
              </p>
            </article>

            <!-- reclaimable movies -->
            <article
              class="bg-card rounded-lg border border-border p-5 min-h-28"
            >
              <p class="text-sm text-muted-foreground">Reclaimable (Movies)</p>
              <p class="text-3xl font-bold text-green-500 mt-2">
                {formatSize(dashboard.kpis.reclaimable_movies_gb)}
              </p>
              <p class="text-xs text-muted-foreground mt-2">
                {reclaimableMovieShare}% of reclaimable total
              </p>
            </article>

            <!-- reclaimable series -->
            <article
              class="bg-card rounded-lg border border-border p-5 min-h-28"
            >
              <p class="text-sm text-muted-foreground">Reclaimable (Series)</p>
              <p class="text-3xl font-bold text-green-500 mt-2">
                {formatSize(dashboard.kpis.reclaimable_series_gb)}
              </p>
              <p class="text-xs text-muted-foreground mt-2">
                {reclaimableSeriesShare}% of reclaimable total
              </p>
            </article>
          </section>

          <section class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
            <!-- total reclaimable -->
            <article
              class="bg-card rounded-lg border border-border p-5 min-h-24"
            >
              <p class="text-sm text-muted-foreground">Total Reclaimable</p>
              <p class="text-2xl font-bold text-primary mt-2">
                {formatSize(dashboard.kpis.reclaimable_total_gb)}
              </p>
              <p class="text-xs text-muted-foreground mt-2">
                {dashboard.kpis.reclaimable_total_gb > 0
                  ? "Cleanup opportunity available"
                  : "Nothing reclaimable right now"}
              </p>
            </article>

            <!-- pending requests -->
            <article
              class="bg-card rounded-lg border border-border p-5 min-h-24"
            >
              <p class="text-sm text-muted-foreground">Pending Requests</p>
              <p class="text-2xl font-bold text-yellow-500 mt-2">
                {dashboard.requests.pending_count}
              </p>
              <p class="text-xs text-muted-foreground mt-2">
                {dashboard.requests.mine_pending} assigned to you
              </p>
            </article>

            <!--  -->
            <article
              class="bg-card rounded-lg border border-border p-5 min-h-24"
            >
              <p class="text-sm text-muted-foreground">Approved (7d)</p>
              <p class="text-2xl font-bold text-green-500 mt-2">
                {dashboard.requests.approved_7d}
              </p>
              <div
                class="mt-2 flex items-center gap-1 text-xs text-muted-foreground"
              >
                <TrendingUp class="w-3.5 h-3.5 text-green-500" />
                Last 7 days
              </div>
            </article>

            <!-- denied requests -->
            <article
              class="bg-card rounded-lg border border-border p-5 min-h-24"
            >
              <p class="text-sm text-muted-foreground">Denied (7d)</p>
              <p class="text-2xl font-bold text-destructive mt-2">
                {dashboard.requests.denied_7d}
              </p>
              <div
                class="mt-2 flex items-center gap-1 text-xs text-muted-foreground"
              >
                <TrendingDown class="w-3.5 h-3.5 text-destructive" />
                Last 7 days
              </div>
            </article>
          </section>

          <!-- request balance (7d) -->
          <section>
            <div
              class="inline-flex items-center gap-2 rounded-md border border-border bg-secondary/20 px-3
                py-1.5 text-sm"
            >
              {#if requestBalance7d > 0}
                <TrendingUp
                  class={`size-5 ${getTrendClass(requestBalance7d)}`}
                />
              {:else if requestBalance7d < 0}
                <TrendingDown
                  class={`size-5 ${getTrendClass(requestBalance7d)}`}
                />
              {:else}
                <Minus class={`size-5 ${getTrendClass(requestBalance7d)}`} />
              {/if}
              <span class="text-muted-foreground">Request balance (7d):</span>
              <span class={`font-medium ${getTrendClass(requestBalance7d)}`}>
                {formatSigned(requestBalance7d)}
              </span>
            </div>
          </section>

          <!-- my requests -->
          <section class="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <article class="bg-card rounded-lg border border-border p-5">
              <h2 class="text-lg font-semibold text-foreground mb-4">
                My Requests
              </h2>
              <div class="space-y-3">
                <div class="flex items-center justify-between">
                  <span class="text-muted-foreground">Active</span>
                  <span class="font-semibold text-foreground">
                    {dashboard.requests.mine_active}
                  </span>
                </div>
                <div class="flex items-center justify-between">
                  <span class="text-muted-foreground">Pending</span>
                  <span class="font-semibold text-foreground">
                    {dashboard.requests.mine_pending}
                  </span>
                </div>
              </div>
            </article>

            <!-- admin services -->
            {#if isAdmin}
              <article
                class="bg-card rounded-lg border border-border p-5 lg:col-span-2"
              >
                <h2 class="text-lg font-semibold text-foreground mb-4">
                  Services
                </h2>
                {#if dashboard.services.length === 0}
                  <p class="text-muted-foreground">No services configured.</p>
                {:else}
                  <div
                    class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3"
                  >
                    {#each dashboard.services as service (service.name)}
                      <div
                        class="rounded-md border border-border bg-secondary/20 p-3 min-w-0"
                      >
                        <div class="flex items-center justify-between gap-2">
                          <p class="font-medium text-foreground truncate">
                            {service.name}
                          </p>
                          <span
                            class="text-xs px-2 py-0.5 rounded-full {service.enabled
                              ? 'bg-green-500/20 text-green-500'
                              : 'bg-muted text-muted-foreground'}"
                          >
                            {service.status}
                          </span>
                        </div>
                        {#if shouldShowServiceSync(service.name)}
                          <p
                            class="text-xs text-muted-foreground mt-2 truncate"
                          >
                            Last sync: {service.last_sync_at
                              ? formatDistanceToNow(service.last_sync_at)
                              : "never"}
                          </p>
                        {/if}
                      </div>
                    {/each}
                  </div>
                {/if}
              </article>
            {/if}
          </section>

          <!-- recent activity -->
          <section class="bg-card rounded-lg border border-border p-5">
            <h2 class="text-xl font-semibold text-foreground mb-4">
              Recent Activity
            </h2>

            {#if activity.length === 0}
              <p class="text-muted-foreground text-center py-8">
                No recent activity
              </p>
            {:else}
              <div class="max-h-96 divide-y overflow-y-auto divide-border pr-3">
                {#each activity as item (item.id)}
                  <div class="py-3">
                    <div class="flex items-start justify-between gap-3">
                      <div class="min-w-0">
                        <div class="flex items-center gap-2 min-w-0">
                          <p
                            class="text-sm font-medium text-foreground truncate"
                          >
                            {getActivityTitle(item)}
                          </p>
                          <span
                            class={`text-[11px] px-2 py-0.5 rounded-full shrink-0 ${getActivityBadgeClass(item)}`}
                          >
                            {getActivityLabel(item)}
                          </span>
                        </div>
                        {#if getActivitySubtitle(item)}
                          <p class="text-sm text-muted-foreground truncate">
                            {getActivitySubtitle(item)}
                          </p>
                        {/if}
                      </div>
                      <div class="text-right shrink-0">
                        <p class="text-xs text-muted-foreground">
                          {formatDistanceToNow(item.created_at)}
                        </p>
                        <p class="text-xs text-muted-foreground/80">
                          {formatDate(item.created_at)}
                        </p>
                      </div>
                    </div>
                  </div>
                {/each}
              </div>
            {/if}
          </section>
        </div>
      {/if}
    {/if}
  </div>
</div>
