import assert from "node:assert/strict";
import test from "node:test";

import { EvoAgentClient, EvoAgentError } from "../dist/index.js";

test("uses canonical demo and approval routes with idempotency", async () => {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(
      JSON.stringify(
        url.endsWith("/demo/launch")
          ? { workflow_id: "wf_1", run_id: "run_1", approval_id: "fleet:wf_1:publish" }
          : { approved: true },
      ),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  const client = new EvoAgentClient({ baseUrl: "https://control.example/", token: "token", fetch });
  const demo = await client.launchDemo({ idempotencyKey: "demo-1" });
  await client.decideApproval(demo.approval_id, { approved: true, actor: "operator" });

  assert.equal(calls[0].url, "https://control.example/api/v1/demo/launch");
  assert.equal(calls[0].init.headers["Idempotency-Key"], "demo-1");
  assert.equal(calls[0].init.headers.Authorization, "Bearer token");
  assert.equal(
    calls[1].url,
    "https://control.example/api/v1/approvals/fleet%3Awf_1%3Apublish",
  );
});

test("surfaces structured HTTP failures", async () => {
  const fetch = async () =>
    new Response(JSON.stringify({ detail: "denied" }), {
      status: 403,
      headers: { "content-type": "application/json" },
    });
  const client = new EvoAgentClient({ baseUrl: "https://control.example", fetch });

  await assert.rejects(
    () => client.overview(),
    (error) => error instanceof EvoAgentError && error.status === 403,
  );
});
