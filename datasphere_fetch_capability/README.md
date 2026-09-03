# Datasphere Fetch — Joule pro-code (A2A) capability

Joule extension capability that delegates natural-language questions about
SAP Datasphere object-usage data to the remote A2A agent this repo deploys
(`app.py` → `a2a_server.py` → `agent.py`).

Structure and wire contract follow SAP's
[Joule A2A agent toolkit](https://github.com/SAP-samples/joule-a2a-agent-toolkit)
and the blog [*Joule A2A: Connect Code Based Agents into Joule*](https://community.sap.com/t5/technology-blog-posts-by-sap/joule-a2a-connect-code-based-agents-into-joule/ba-p/14329279).
Earlier reference: `../../smart_material_ai/smart_material_capability/`.

## Layout

| File | Purpose |
|---|---|
| `capability.sapdas.yaml` | Metadata + `system_aliases` (one: `DATASPHERE_FETCH`). `schema_version: 3.28.0` — the minimum for A2A `agent-request`. |
| `da.sapdas.yaml` | Deploy-artifact descriptor (`schema_version: 1.4.0`). |
| `capability_context.yaml` | Declares `contextId` / `taskId` so multi-turn conversations thread. |
| `functions/querydatasphereusage.yaml` | `agent-request` → `DATASPHERE_FETCH`; reads `result.body.status.message.parts[0].text`. |
| `scenarios/query_datasphere_usage.yaml` | NL phrasings that route to the function; threads context back. |

## The `DATASPHERE_FETCH` destination

`system_aliases.DATASPHERE_FETCH.destination` resolves to a BTP destination
of the same name. **It must point at the A2A mount root, `…/a2a` — not
`…/a2a/ask`, not the bare app root.** Joule discovers the agent card at
`<URL>/.well-known/agent-card.json` and also POSTs JSON-RPC to `<URL>`; the
app serves both at `/a2a`.

```
Name=DATASPHERE_FETCH
Type=HTTP
URL=https://datasphere-fetch.cfapps.us10-001.hana.ondemand.com/a2a
ProxyType=Internet
Authentication=NoAuthentication
# Additional Properties
HTML5.DynamicDestination=true
WebIDEEnabled=true
```

> The screenshot-created destination pointed at `…/a2a/ask`. Edit it to
> `…/a2a` and add the two Additional Properties above.

Sanity-check after `cf push`:

```bash
curl https://datasphere-fetch.cfapps.us10-001.hana.ondemand.com/a2a/.well-known/agent-card.json
```

## Build & deploy the capability

```bash
npm install -g @sap/joule-cli
joule login

# from this folder — compile (validate against schema) + deploy a test assistant
joule deploy ./da.sapdas.yaml --compile -n "datasphere_fetch_assistant"

joule list
joule launch "datasphere_fetch_assistant"
```

Bump `metadata.version` in `capability.sapdas.yaml` on every redeploy.

## Wire-format contract (why `a2a_server.py` looks the way it does)

- Emits **one already-completed** A2A `Task`; the answer is in
  `status.message.parts[0].text` — the exact path the function reads. A
  submitted/working Task emitted first would be captured by Joule's
  `agent-request` as a message-less result.
- JSON-RPC is served at the mount root with `enable_v0_3_compat=True`
  (Joule uses the legacy `message/send` method, no `A2A-Version` header).
- `app.py` also binds a bare `POST /a2a` route so Joule's non-redirect-
  following POST client reaches the handler without the Mount's 307.
- The agent itself is stateless — `contextId`/`taskId` are threaded for A2A
  task correlation, but `agent.py` does not yet retain prior-turn memory.
