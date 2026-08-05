# Architecture

```mermaid
flowchart LR
  U[Operator / API] --> C[Control plane]
  C --> D[(Workflow + event store)]
  C --> Q[Ready-node queue]
  Q --> M[Capability and concurrency match]
  M --> L[Expiring lease]
  L --> W1[Research worker]
  L --> W2[Code worker]
  L --> W3[Review worker]
  W1 --> A[(Content-addressed artifacts)]
  W2 --> A
  W3 --> A
  C --> H[Approval gate]
  C --> R[Route metrics]
```

The workflow database is authoritative. A node becomes ready only after all dependencies complete. Claim uses a conditional update, making the lease token the commit capability. Heartbeats extend that lease; completion, failure and late-result rejection are durable transitions.

The local reference uses SQLite WAL. A multi-replica deployment should move claims to PostgreSQL `FOR UPDATE SKIP LOCKED` or a durable workflow engine while preserving the API invariants.

