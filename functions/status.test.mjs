// node --test functions/  (no packages needed)
import assert from "node:assert/strict";
import test from "node:test";
import { toStatus } from "./_lib/status.js";

const summary = {
  page: { updated_at: "2026-11-20T07:39:52.176Z" },
  status: { indicator: "major", description: "Partial System Outage" },
  components: [
    { id: "a", name: "Claude API (api.anthropic.com)", status: "major_outage", group: false },
    { id: "g", name: "Claude on Cloud Platforms", status: "operational", group: true },
    { id: "c", name: "Claude Code", status: "operational", group: false, group_id: null },
  ],
  incidents: [
    { id: "i1", name: "Elevated errors", status: "investigating", impact: "major",
      created_at: "2026-11-20T07:00:00Z", started_at: "2026-11-20T06:58:00Z", shortlink: "https://stspg.io/x" },
  ],
};
const incidents = { incidents: Array.from({ length: 12 }, (_, n) => ({ id: `r${n}`, name: `Old ${n}`,
  status: "resolved", impact: "minor", created_at: "2026-11-01T00:00:00Z", resolved_at: "2026-11-01T01:00:00Z" })) };

test("keeps components, drops group headers, lists open and recent incidents", () => {
  const out = toStatus(summary, incidents, new Date("2026-11-20T08:00:00Z"));
  assert.equal(out.indicator, "major");
  assert.deepEqual(out.components.map((c) => c.name), ["Claude API (api.anthropic.com)", "Claude Code"]);
  assert.equal(out.active_incidents.length, 1);
  assert.equal(out.active_incidents[0].started_at, "2026-11-20T06:58:00Z");
  assert.equal(out.recent_incidents.length, 10);
  assert.equal(out.fetched_at, "2026-11-20T08:00:00.000Z");
});

test("unknown indicators never reach the app", () => {
  assert.equal(toStatus({ status: { indicator: "weird" } }, {}).indicator, "unknown");
});
