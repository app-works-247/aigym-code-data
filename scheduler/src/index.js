// Cloudflare Worker that starts the data workflows on time.
//
// GitHub's own `schedule:` trigger ran late or not at all on 2026-11-21 (nightly 02:00 never
// started, official skipped 03:30, docs-watch 05:15 never started). Cloudflare Cron Triggers
// call this Worker at the times below; it asks GitHub to run the workflow (workflow_dispatch).
// The workflows keep `workflow_dispatch`, so they can still be started by hand.
//
// Secret: GH_DISPATCH_TOKEN, a fine-grained token for app-works-247/aigym-code-data with only
// "Actions: Read and write". Nothing else is stored here.

export const REPO = "app-works-247/aigym-code-data";

// Cron expression (UTC) -> workflow file. Must match `triggers.crons` in wrangler.toml.
export const SCHEDULE = {
  "17 2 * * *": "nightly.yml", // 09:17 in Vietnam
  "30 */3 * * *": "official.yml", // every 3 hours
  "15 5 * * *": "docs-watch.yml", // 12:15 in Vietnam
  "47 3 * * SUN": "weekly.yml", // encrypted database backup, Sunday 10:47 in Vietnam
};

const RETRY_STATUSES = new Set([500, 502, 503, 504]);

/**
 * Ask GitHub to run one workflow on main. Resolves with the HTTP status (204 = accepted).
 * Retries once on a GitHub server error or a network failure; throws when it still fails,
 * so the run shows as failed in Cloudflare's Cron Events.
 */
export async function dispatch(workflow, token, { fetchImpl = fetch, wait = sleep } = {}) {
  if (!token) throw new Error("GH_DISPATCH_TOKEN is not set");
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`;
  const init = {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "aigym-scheduler",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  };
  let last;
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      const res = await fetchImpl(url, init);
      if (res.status === 204) return 204;
      const text = (await res.text()).slice(0, 300);
      last = new Error(`GitHub answered ${res.status} for ${workflow}: ${text}`);
      if (!RETRY_STATUSES.has(res.status)) break;
    } catch (err) {
      last = err;
    }
    if (attempt === 1) await wait(20_000);
  }
  throw last;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export default {
  // Awaited, not handed to ctx.waitUntil: a failed dispatch then fails this invocation, so it
  // shows as an error in the Worker's Cron Events instead of passing silently.
  async scheduled(event, env) {
    const workflow = SCHEDULE[event.cron];
    if (!workflow) throw new Error(`No workflow for cron "${event.cron}"`);
    await dispatch(workflow, env.GH_DISPATCH_TOKEN);
    console.log(`started ${workflow}`);
  },

  // Nothing to serve: the Worker only runs on its schedule.
  async fetch() {
    return new Response("Not found", { status: 404 });
  },
};
