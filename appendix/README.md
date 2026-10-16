# Architectural brief

This note explains how the RHOAI 3.5.1 MaaS tokenomics extension works and why it is shaped this way. Install steps, commands, and the object-by-object change list stay in the [repository README](../README.md) and in [MAAS_MODIFICATIONS.md](../MAAS_MODIFICATIONS.md).

The pricing page is a demonstration of these capabilities. It is not a supported user interface. The page says that in a banner.

## The problem

Models-as-a-Service already counts traffic at the gateway. The metric `authorized_hits` is one combined token total. It does not split input, cache, and decoded tokens, and it does not attribute that split to an OpenShift user and project.

Showback needs those three counts, a price for each, and a view that a cluster user can open without receiving someone else's usage. The product Usage dashboard (`dashboard-3-maas-usage-admin`) already exists for the gateway view. This extension does not replace it.

## What this repository does not do

- It does not patch a MaaS Deployment, the gateway, Authorino, Limitador, or llm-d.
- It does not register the pricing page inside the OpenShift AI dashboard module federation. That registration would change `rhods-dashboard`.
- It does not edit `dashboard-3-maas-usage-admin`. The Tokenomics and Cost per token dashboards are additional objects.
- It does not turn MLflow, LiteLLM, or pod-log reading on. Those readers exist and stay idle until an operator sets them.
- It does not call an external provider. A row in the cost matrix is a price, not a connection.

## Shape

```text
caller
  │
  ├─ local model through the MaaS gateway ──► vLLM counters (no user)
  │                                      └──► usage object, if a client posts it
  │
  └─ ExternalModel through the gateway ──────► usage object, only if something posts it
                                               (vLLM never sees this call)

usage object ──► POST /v1/usage ──► exporter ──► maas_user_token_usage_total
                                                      │
                                                      ▼
                                              Thanos Querier
                                                      │
                                                      ▼
                                         pricing page (oauth-proxy)
                                         prices the row from the matrix
```

Two processes run in `maas-finops-ui`. The exporter records usage and exposes Prometheus text. The pricing UI reads Thanos, stores the price catalog, and serves the page. They do not share a database. Prometheus is the usage store. A JSON file on the UI volume is the price store.

Shared parsing and cost math live in `tokenomics/` so the exporter and the UI do not each invent a token type.

## How a call becomes a cost

1. A client calls the MaaS gateway with a model id such as `publishers/llm/models/gpt-oss-20b`. The gateway authenticates that call with the caller's MaaS key. The key is not an OpenShift user, so the gateway response does not carry the OpenShift username.
2. If the caller, or a script that verified `oc get user`, posts the OpenAI `usage` object to `POST /v1/usage`, the exporter records input, cache, and decoded tokens with labels `user`, `project`, `token_type`, and `model`. The ingest token is required. The process refuses to start without it.
3. User-workload monitoring scrapes that series. The pricing page queries Thanos for the selected span (1h, 24h, 7d, 30d).
4. When that series is empty, the page reads the local vLLM counters `kserve_vllm:prompt_tokens_by_source_total` and `kserve_vllm:generation_tokens_total`. Those rows are labeled `all-callers`. They are model-server totals, not per-user showback.
5. Each usage row is priced by the matrix row whose model name matches. `*` is the showback row and prices every other model. The comparison table multiplies the tokens already on screen by every provider's rates. That table does not discover traffic.

Cache tokens that are already inside `prompt_tokens` (the OpenAI and vLLM shape, where `total_tokens = prompt_tokens + completion_tokens`) are subtracted from input and billed only at the cache rate. Decoded tokens are completion tokens.

The increase between Prometheus samples is reset-aware. A counter drop is a new baseline, not a negative bill. The page does not invent tokens from before the first scrape in the span, and it does not invent tokens from before the exporter existed.

## Decisions

### Leave the MaaS workloads alone

The extension had to be removable and reviewable next to a supported MaaS install. A sidecar, a mutated gateway, or a change to `rhods-dashboard` would mix this code into the product lifecycle. New objects in `maas-finops-ui`, plus a few labeled dashboards and read-only RBAC, can be deleted without undeploying MaaS. Optional changes are listed separately and stay off unless an operator asks for them.

Phase 5 is the same idea applied to the demo. `./deploy.sh` runs phases 1–4. Catalogs, the research agent, MCP servers, and Open WebUI are a separate `./deploy.sh 5`. The HTPasswd helper is not called by `deploy.sh`. If the cluster already has an identity provider, that helper prints the provider names and exits. It adds `tokenomics-htpasswd` only when kubeadmin is the only login, because replacing an existing provider such as `rhbk` would break the cluster login.

### Record three token types outside the gateway metric

`authorized_hits` cannot answer input versus cache versus decoded. vLLM can, on the pod that served a local model, and an OpenAI `usage` object can, wherever the client received one. The exporter's job is to turn that object into a stable series. It is a separate Deployment so a MaaS rollout does not take the counters with it, and so the exporter can be scraped without opening the gateway pods.

The exporter starts idle. MLflow, LiteLLM spend logs, and pod logs are readers, not the default path. OpenShift AI does not ship either MLflow or LiteLLM as the MaaS usage API. Leaving them off means a fresh install does not poll services that are not there. An operator turns a reader on by setting its URL or `ENABLE_POD_LOGS`. Pod logs also need an explicit Role; `deploy.sh` does not apply it.

`POST /v1/usage` is the path that can name an OpenShift user. The research demo uses it that way: `oc --as` runs the question as the verified user, the agent returns the gateway `usage` object, and the script posts that object from inside the exporter pod. The post is local because the exporter NetworkPolicy admits monitoring, the pricing namespace, and `openshift-logging`. It does not admit `test-project`. The user on the series is the verified OpenShift name, not the shared MaaS key.

An external model registered on MaaS (`MaaSModelRef` of kind `ExternalModel`) is proxied by the gateway and never hits a vLLM pod. Those tokens show up only when a usage object is posted or a reader is enabled and sees one. A typed provider such as `gpt-4o` does not open that endpoint.

### The metric label is `project`, not `namespace`

OpenShift writes the scrape namespace onto the `namespace` label even when a ServiceMonitor sets `honorLabels: true`. Putting the caller's project in `namespace` would replace it with `maas-finops-ui` and every user would look like they ran in the exporter namespace. The series keeps the OpenShift project in `project`. The page reads `project` first.

### Cost math stays a pure function

Rates are stored per token. The page also shows the same number per million tokens, which is how catalogs are usually written. `tokenomics/cost.py` is the only place the formula lives:

```text
input_cost   = input_tokens   * input_rate_per_token
cache_cost   = cache_tokens   * cache_rate_per_token
decoded_cost = decoded_tokens * decoded_rate_per_token
```

That split follows the distribution-center `llm_cost.py` helper, which prices tokens from a dollars-per-million rate on an MLflow span. Showback needs three rates and must not require a span, so the division moved here and the span writer did not. The UI and the exporter both call this function. A later pricing change should not fork the formula into the page.

The matrix is a catalog on the UI volume (`pricing.json`), written by an administrator. One row is the showback row (`local: true`, model `*` unless a specific model is chosen). An exact, case-insensitive model match wins. Everything else uses that showback row. A file saved before the matrix existed, with only `rates`, becomes one Local MaaS row so old catalogs still load.

The model dropdown lists what MaaS is serving by reading `MaaSModelRef`. The UI service account has `get` and `list` on that resource and nothing else. The option list includes `status.resolvedModelAlias` (`publishers/llm/models/gemma-2-9b-it`) and, when it differs, `spec.modelRef.name` (`gemma-2-9b-it`). Both are offered because matching is exact: gateway clients send the alias, and the vLLM fallback labels rows with the served name. Other keeps a text field for a name that is not registered. The list is cached for about a minute. If the API cannot be reached, the field still offers `*` and Other.

`rhai_api_pricing_mtok` is not published by this extension. The Cost per token dashboard's API price columns stay empty unless that series already exists. The page does not invent it.

### The pricing page trusts the proxy, then the catalog

The UI process listens on `127.0.0.1`. The OpenShift `oauth-proxy` sidecar is the only listener on the Route. The Route uses reencrypt TLS. A browser reaches the proxy, the proxy reaches the app, and a direct call from another pod does not.

The proxy passes `X-Forwarded-User` and `X-Forwarded-Groups`. It does not forward the user's OpenShift token as `Authorization`. The app treats a bearer token as the break-glass administrator secret only. A wrong bearer is rejected and is not retried as the forwarded user. That keeps a stolen or stale token from being ignored in favor of a header the client could also set if it could reach the process directly. Because the process is on localhost, the header is meaningful only when the proxy set it.

The page asks `/api/me` without a bearer first. A stored break-glass token is used only when OpenShift has no user. Cluster users are not prompted for the secret. The token remains for an operator who is not coming through the proxy.

A signed-in user sees rows whose `user` label matches their username. An administrator sees every user and project and can save prices. Administrator means membership in `system:cluster-admins`, `cluster-admins`, `rhods-admins`, or `dedicated-admins`, an `ADMIN_USERS` entry, or a SubjectAccessReview that shows cluster-admin wildcard access. The review Role allows `create` on `subjectaccessreviews`. It does not grant admin rights.

Prices are group-writable on a PVC with a Recreate strategy so one process owns the file. The UI runs as non-root, with the runtime default seccomp profile, all capabilities dropped, and no pinned user id, which is what a restricted pod security admission expects.

### Dashboards are added, not merged into Usage

OpenShift AI Observability selects Perses dashboards labeled `app.opendatahub.io/dashboard=true` and `platform.opendatahub.io/part-of=dashboard`. Tokenomics and Cost per token carry those labels. Usage keeps the product labels and the product queries.

Cost per token reads cluster Prometheus through `cluster-prometheus-datasource` (Thanos), scoped by namespace and model name. It shows the vLLM counters. It does not show per-user exporter rows and it does not fill from the pricing matrix.

The Perses operator that publishes those objects runs in `openshift-operators`. The product NetworkPolicy on the Perses service allows `openshift-cluster-observability-operator`, which has no pods on this layout. An additional policy admits the operator pods labeled `app.kubernetes.io/name=perses-operator` to port 8080. Without it the dashboards stay unpublished. The policy does not widen access for the pricing UI or the exporter.

Grafana remains a second view of the same exporter series, for the OpenShift console dashboard menus. It is the community Grafana operator and a UBI-based Grafana image. OpenShift AI Observability does not read it. Both views exist because the console and the OpenShift AI UI select different objects.

### Images and the checkpoint

Every image this repository builds starts `FROM registry.access.redhat.com/ubi`. Builds run as OpenShift `BuildConfig`s and push to the internal registry. The running tag is `IfNotPresent`, so a new build of the same tag is rolled by setting the deployment to the new digest.

The exporter keeps a SQLite checkpoint on its PVC. Prometheus resets are visible as counter drops, and the checkpoint stops the process from appending the same ingest event again after a restart. SQLite here is the Python standard library on the volume, not a separate database service.

## Trust boundary

| Boundary | What crosses it |
| --- | --- |
| Browser to Route | OpenShift login. The app is not on the Route by itself. |
| Proxy to UI | Forwarded user and groups. Bearer only if the page sends the break-glass token. |
| UI to API server | SubjectAccessReview, and a list of `MaaSModelRef`. No writes to MaaS. |
| UI to Thanos | Read of usage series, with the UI service account's monitoring view. |
| Client to exporter | `POST /v1/usage` with the ingest token. |
| Exporter to Prometheus | Scrape of `/metrics` from user-workload monitoring. |

A MaaS API key is not a principal on the pricing page. Attribution is only as good as the `user` value on the usage event. If that value is a shared key or `all-callers`, the page shows that label and a normal user will not see the row as their own.
