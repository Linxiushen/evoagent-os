# Operations Guide

Monitor queued age, lease expiry rate, terminal failure rate, retries per node, budget rejection, approval wait, workflow critical-path time, artifact volume and route-score drift. Alert when no eligible worker exists for a queued capability or a pool's rolling success rate drops.

Back up the SQLite database and artifact directory together. Events provide an audit trail, but they are not currently sufficient to rebuild every table. Use one control-plane process with SQLite. Configure external authentication and TLS at a reverse proxy before exposing the API.

Do not place model provider keys in workflow input. Workers should obtain scoped credentials from their own secret manager and publish evidence rather than secrets as artifacts.

