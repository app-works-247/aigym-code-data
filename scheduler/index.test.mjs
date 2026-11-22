// node --test scheduler/index.test.mjs  (no packages needed)
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import test from "node:test";
import worker, { REPO, SCHEDULE, dispatch } from "./src/index.js";

const here = new URL(".", import.meta.url);
const noWait = async () => {};

function fakeFetch(responses) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    const next = responses.shift();
    if (next instanceof Error) throw next;
    return new Response(next.body ?? null, { status: next.status });
  };
  return { impl, calls };
}

test("sends a workflow_dispatch on main with the token", async () => {
  const { impl, calls } = fakeFetch([{ status: 204 }]);
  assert.equal(await dispatch("official.yml", "tok", { fetchImpl: impl, wait: noWait }), 204);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `https://api.github.com/repos/${REPO}/actions/workflows/official.yml/dispatches`);
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.Authorization, "Bearer tok");
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main" });
});

test("retries once on a GitHub server error or a network failure", async () => {
  for (const first of [{ status: 502, body: "bad gateway" }, new TypeError("network down")]) {
    const { impl, calls } = fakeFetch([first, { status: 204 }]);
    assert.equal(await dispatch("nightly.yml", "tok", { fetchImpl: impl, wait: noWait }), 204);
    assert.equal(calls.length, 2);
  }
});

test("does not retry a refused token and reports why", async () => {
  const { impl, calls } = fakeFetch([{ status: 403, body: '{"message":"Resource not accessible"}' }]);
  await assert.rejects(dispatch("nightly.yml", "tok", { fetchImpl: impl, wait: noWait }), /403.*Resource not accessible/);
  assert.equal(calls.length, 1);
});

test("fails after the second server error", async () => {
  const { impl, calls } = fakeFetch([{ status: 503 }, { status: 503 }]);
  await assert.rejects(dispatch("nightly.yml", "tok", { fetchImpl: impl, wait: noWait }), /503/);
  assert.equal(calls.length, 2);
});

test("refuses to run without a token", async () => {
  await assert.rejects(dispatch("nightly.yml", "", { fetchImpl: async () => assert.fail(), wait: noWait }), /GH_DISPATCH_TOKEN/);
});

test("every cron in wrangler.toml has a workflow that exists", () => {
  const toml = readFileSync(new URL("wrangler.toml", here), "utf8");
  const crons = JSON.parse(toml.match(/^crons = (\[.*\])$/m)[1]);
  assert.deepEqual([...crons].sort(), Object.keys(SCHEDULE).sort());
  const workflows = readdirSync(new URL("../.github/workflows/", here));
  for (const file of Object.values(SCHEDULE)) assert.ok(workflows.includes(file), file);
});

test("a failed dispatch fails the scheduled run, so Cron Events shows it", async () => {
  await assert.rejects(worker.scheduled({ cron: "47 3 * * SUN" }, {}, { waitUntil() {} }), /GH_DISPATCH_TOKEN/);
});

test("an unknown cron is an error, not a silent no-op", async () => {
  await assert.rejects(worker.scheduled({ cron: "0 0 * * *" }, {}, { waitUntil() {} }), /No workflow/);
});
