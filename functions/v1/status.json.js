// Cloudflare Pages Function serving https://aigym-code.pages.dev/v1/status.json.
import { CACHE_SECONDS, INCIDENTS_URL, SUMMARY_URL, toStatus } from "../_lib/status.js";

async function getJSON(url) {
  const response = await fetch(url, {
    headers: { "User-Agent": "aigym-code-status" },
    cf: { cacheTtl: CACHE_SECONDS, cacheEverything: true }, // at most one upstream call per 5 min
  });
  if (!response.ok) throw new Error(`${url} answered ${response.status}`);
  return response.json();
}

function reply(body, status, maxAge) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": `public, max-age=${maxAge}`,
      "Access-Control-Allow-Origin": "*",
    },
  });
}

export async function onRequestGet() {
  try {
    const [summary, incidents] = await Promise.all([getJSON(SUMMARY_URL), getJSON(INCIDENTS_URL)]);
    return reply(toStatus(summary, incidents), 200, CACHE_SECONDS);
  } catch (error) {
    // The app keeps its last copy and says the status could not be checked.
    return reply({ schema: 1, indicator: "unknown", error: "status.claude.com unavailable" }, 502, 60);
  }
}
