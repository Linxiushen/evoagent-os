# EvoAgent OS TypeScript SDK

```ts
import { EvoAgentClient } from "@evoagent-os/sdk";

const client = new EvoAgentClient({ baseUrl: "http://127.0.0.1:8765" });
const demo = await client.launchDemo({ idempotencyKey: "market-brief-1" });
await client.decideApproval(demo.approval_id, { approved: true, actor: "release-manager" });
```

The SDK uses the platform `fetch` implementation and has no runtime dependencies.
