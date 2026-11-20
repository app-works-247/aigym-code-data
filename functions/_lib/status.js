// Shape status.claude.com (Atlassian Statuspage v2) into /v1/status.json for the app.
// Decision 15: service status goes through Cloudflare, not the pipeline, so an incident shows
// within minutes (PLAN.md 12.2: at most 15 minutes) without keeping the database awake.

export const SUMMARY_URL = "https://status.claude.com/api/v2/summary.json";
export const INCIDENTS_URL = "https://status.claude.com/api/v2/incidents.json";
export const CACHE_SECONDS = 300;

const INDICATORS = new Set(["none", "minor", "major", "critical", "maintenance"]);

function incident(i) {
  return {
    id: i.id,
    name: i.name,
    status: i.status,
    impact: i.impact,
    started_at: i.started_at || i.created_at,
    resolved_at: i.resolved_at || null,
    url: i.shortlink || null,
  };
}

export function toStatus(summary, incidents, now = new Date()) {
  const indicator = INDICATORS.has(summary?.status?.indicator) ? summary.status.indicator : "unknown";
  const components = (summary.components || [])
    .filter((c) => !c.group) // group headers repeat their members
    .map((c) => ({ id: c.id, name: c.name, status: c.status }));
  return {
    schema: 1,
    fetched_at: now.toISOString(),
    updated_at: summary?.page?.updated_at || null,
    indicator,
    description: summary?.status?.description || "",
    components,
    active_incidents: (summary.incidents || []).map(incident),
    recent_incidents: (incidents?.incidents || []).slice(0, 10).map(incident),
    page: "https://status.claude.com",
  };
}
