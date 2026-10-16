import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDERS = {
    "__FINOPS_NAMESPACE__": "maas-finops",
    "__UI_NAMESPACE__": "maas-finops-ui",
    "__OBS_NAMESPACE__": "redhat-ods-monitoring",
    "__MAAS_NAMESPACE__": "models-as-a-service",
    "__EXPORTER_IMAGE__": "registry.example/exporter:0.1.0",
    "__UI_IMAGE__": "registry.example/ui:0.1.0",
    "__OAUTH_PROXY_IMAGE__": "registry.example/oauth-proxy:v4.18",
    "__GRAFANA_LABEL__": "grafana",
    "__GRAFANA_IMAGE__": "image-registry.example/maas-finops/maas-tokenomics-grafana:0.1.0",
    "__PERSES_DATASOURCE__": "data-science-prometheus-datasource",
    "__CARAVAN_NAMESPACE__": "test-project",
    "__CARAVAN_AGENT_IMAGE__": "registry.example/caravan-agent:dev1",
    "__CARAVAN_BASE_IMAGE__": "image-registry.example/openshift/python:3.12-ubi9",
    "__OPENWEBUI_IMAGE__": "registry.example/open-webui:0.1.0",
    "__RESEARCHER_IMAGE__": "registry.example/researcher-agent:0.1.0",
    "__OPENAI_BASE_URL__": "https://maas.example/v1",
    "__OPENAI_MODEL__": "example-model",
    "__RESEARCHER_MODEL__": "example-researcher-model",
}


def render(text: str) -> str:
    for key, value in PLACEHOLDERS.items():
        text = text.replace(key, value)
    return text


def yaml_files() -> list[Path]:
    return sorted((ROOT / "deploy").rglob("*.yaml"))


@pytest.mark.parametrize("path", yaml_files(), ids=lambda path: str(path.relative_to(ROOT)))
def test_manifests_parse(path: Path) -> None:
    rendered = render(path.read_text(encoding="utf-8"))
    for token in PLACEHOLDERS:
        assert token not in rendered
    documents = list(yaml.safe_load_all(rendered))
    assert documents
    if path.name.endswith("-patch.yaml"):
        assert all(isinstance(doc, dict) and "spec" in doc for doc in documents)
        return
    assert all(isinstance(doc, dict) and doc.get("kind") for doc in documents)


def test_grafana_dashboard_contract() -> None:
    dashboard = json.loads((ROOT / "deploy/phase3/tokenomics.json").read_text(encoding="utf-8"))
    assert dashboard["title"] == "Tokenomics"
    assert dashboard["uid"] == "maas-tokenomics"
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert {"Input tokens", "Cache tokens", "Decoded tokens", "Tokens by user and model"} <= titles
    ranges = {item["display"] for item in dashboard["timepicker"]["quick_ranges"]}
    assert ranges == {"Last 1 hour", "Last 24 hours", "Last 7 days", "Last 30 days"}
    joined = json.dumps(dashboard)
    assert "sum by (user, model, token_type)" in joined
    assert "maas_user_token_usage_total" in joined
    assert 'token_type=\\"input\\"' in joined or 'token_type="input"' in joined


def test_grafana_operator_resources_select_the_same_instance() -> None:
    grafana = next(yaml.safe_load_all(render((ROOT / "deploy/phase3/grafana.yaml").read_text(encoding="utf-8"))))
    dashboard = next(yaml.safe_load_all(render((ROOT / "deploy/phase3/grafanadashboard.yaml").read_text(encoding="utf-8"))))
    datasource = next(yaml.safe_load_all(render((ROOT / "deploy/phase3/grafanadatasource.yaml").read_text(encoding="utf-8"))))
    subscription = next(yaml.safe_load_all((ROOT / "deploy/phase3/operator-subscription.yaml").read_text(encoding="utf-8")))
    labels = grafana["metadata"]["labels"]
    assert labels["dashboards"] == "grafana"
    assert labels["folders"] == "grafana"
    assert grafana["spec"]["version"] == "image-registry.example/maas-finops/maas-tokenomics-grafana:0.1.0"
    assert grafana["metadata"]["namespace"] == "maas-finops"
    assert dashboard["metadata"]["namespace"] == grafana["metadata"]["namespace"]
    assert dashboard["spec"]["instanceSelector"]["matchLabels"]["dashboards"] == labels["dashboards"]
    assert datasource["spec"]["instanceSelector"]["matchLabels"]["dashboards"] == labels["dashboards"]
    assert datasource["spec"]["datasource"]["url"].startswith("https://thanos-querier.")
    assert subscription["spec"]["source"] == "community-operators"
    assert subscription["spec"]["channel"] == "v5"
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "deploy/phase3/grafana.yaml" in script
    assert "deploy/phase3/operator-subscription.yaml" in script
    observability = next(yaml.safe_load_all((ROOT / "deploy/phase3/cluster-observability-subscription.yaml").read_text(encoding="utf-8")))
    assert observability["spec"]["source"] == "redhat-operators"
    assert observability["spec"]["channel"] == "stable"
    dashboard_patch = next(yaml.safe_load_all(render((ROOT / "deploy/phase3/dashboard-observability-patch.yaml").read_text(encoding="utf-8"))))
    assert dashboard_patch["spec"]["observability"]["enabled"] is True
    assert dashboard_patch["spec"]["observability"]["persesService"]["namespace"] == "redhat-ods-monitoring"
    dsci_patch = next(yaml.safe_load_all(render((ROOT / "deploy/phase3/dsci-monitoring-patch.yaml").read_text(encoding="utf-8"))))
    assert dsci_patch["spec"]["monitoring"]["metrics"]["storage"]["size"] == "5Gi"
    assert dsci_patch["spec"]["monitoring"]["alerting"] == {}
    assert dsci_patch["spec"]["monitoring"]["traces"]["storage"]["backend"] == "pv"
    assert "deploy/phase3/opentelemetry-subscription.yaml" in script
    assert "deploy/phase3/tempo-subscription.yaml" in script
    assert "deploy/phase3/dashboard-observability-patch.yaml" in script
    assert "data-science-prometheus-cluster-proxy" in script
    assert "observabilityDashboard" in (ROOT / "deploy/phase3/odh-observability-dashboard-patch.yaml").read_text(encoding="utf-8")
    cost = next(yaml.safe_load_all(render((ROOT / "deploy/phase3/cost-per-token-dashboard.yaml").read_text(encoding="utf-8"))))
    assert cost["apiVersion"] == "perses.dev/v1alpha2"
    assert cost["metadata"]["name"] == "dashboard-cost-per-token-admin"
    assert cost["metadata"]["namespace"] == "redhat-ods-monitoring"
    assert cost["metadata"]["labels"]["app.opendatahub.io/dashboard"] == "true"
    assert cost["metadata"]["labels"]["platform.opendatahub.io/part-of"] == "dashboard"
    assert cost["spec"]["config"]["display"]["name"] == "Cost per token"
    rendered_cost = render((ROOT / "deploy/phase3/cost-per-token-dashboard.yaml").read_text(encoding="utf-8"))
    assert "cluster-prometheus-datasource" in rendered_cost
    assert "kserve_vllm:generation_tokens_total" in rendered_cost
    assert "deploy/phase3/cost-per-token-dashboard.yaml" in script
    policy = next(yaml.safe_load_all(render((ROOT / "deploy/phase3/perses-operator-networkpolicy.yaml").read_text(encoding="utf-8"))))
    assert policy["metadata"]["name"] == "tokenomics-perses-operator-access"
    assert policy["metadata"]["namespace"] == "redhat-ods-monitoring"
    assert policy["spec"]["ingress"][0]["ports"][0]["port"] == 8080
    assert policy["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "openshift-operators"
    assert "deploy/phase3/perses-operator-networkpolicy.yaml" in script


def test_images_are_built_from_ubi_containerfiles() -> None:
    containerfiles = [
        ROOT / "exporter/Containerfile",
        ROOT / "ui/Containerfile",
        ROOT / "grafana/Containerfile",
        ROOT / "openwebui/Containerfile",
        ROOT / "researcher/Containerfile",
    ]
    for path in containerfiles:
        first = next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("FROM "))
        assert first.startswith("FROM registry.access.redhat.com/ubi")
    assert not list(ROOT.rglob("Dockerfile"))
    for path in (ROOT / "deploy/build").glob("*-build.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "dockerfilePath:" in text
        assert "Containerfile" in text
        assert "Dockerfile" not in text


def test_open_webui_is_behind_the_openshift_login_sidecar() -> None:
    docs = list(yaml.safe_load_all(render((ROOT / "deploy/caravan/40-open-webui.yaml").read_text(encoding="utf-8"))))
    deployment = docs[0]
    containers = {item["name"]: item for item in deployment["spec"]["template"]["spec"]["containers"]}
    assert containers["open-webui"]["image"] == "registry.example/open-webui:0.1.0"
    assert containers["open-webui"]["env"]
    host = next(item for item in containers["open-webui"]["env"] if item["name"] == "HOST")
    assert host["value"] == "127.0.0.1"
    base = next(item for item in containers["open-webui"]["env"] if item["name"] == "OPENAI_API_BASE_URL")
    assert base["value"] == "https://maas.example/v1"
    urls = next(item for item in containers["open-webui"]["env"] if item["name"] == "OPENAI_API_BASE_URLS")
    assert urls["value"].startswith("https://maas.example/v1;")
    assert urls["value"].endswith("http://researcher-agent.test-project.svc.cluster.local:8080/v1")
    default_model = next(item for item in containers["open-webui"]["env"] if item["name"] == "DEFAULT_MODELS")
    assert default_model["value"] == "example-model"
    persist = next(item for item in containers["open-webui"]["env"] if item["name"] == "ENABLE_PERSISTENT_CONFIG")
    assert persist["value"] == "false"
    trusted = next(item for item in containers["open-webui"]["env"] if item["name"] == "WEBUI_AUTH_TRUSTED_EMAIL_HEADER")
    assert trusted["value"] == "X-Forwarded-User"
    role = next(item for item in containers["open-webui"]["env"] if item["name"] == "DEFAULT_USER_ROLE")
    assert role["value"] == "user"
    assert any("http://127.0.0.1:8080" in arg for arg in containers["oauth-proxy"]["args"])
    assert containers["oauth-proxy"]["image"] == "registry.example/oauth-proxy:v4.18"
    route = next(yaml.safe_load_all(render((ROOT / "deploy/caravan/60-route.yaml").read_text(encoding="utf-8"))))
    assert route["spec"]["tls"]["termination"] == "reencrypt"
    service = next(yaml.safe_load_all(render((ROOT / "deploy/caravan/50-service.yaml").read_text(encoding="utf-8"))))
    assert service["spec"]["ports"][0]["targetPort"] == "https"
    agent = next(yaml.safe_load_all(render((ROOT / "deploy/caravan/30-caravan-agent.yaml").read_text(encoding="utf-8"))))
    agent_key = agent["spec"]["template"]["spec"]["containers"][0]["env"][-1]["valueFrom"]["secretKeyRef"]
    webui_key = next(
        item["valueFrom"]["secretKeyRef"]
        for item in containers["open-webui"]["env"]
        if item["name"] == "OPENAI_API_KEY"
    )
    assert agent_key == webui_key == {"name": "ojoseph-test-agent-secret", "key": "OPENAI_API_KEY"}
    assert agent["metadata"]["name"] == "ojoseph-test-agent"
    agent_text = (ROOT / "deploy/caravan/30-caravan-agent.yaml").read_text(encoding="utf-8")
    assert "name: test-agent-secret" not in agent_text
    assert "caravan-agent" not in agent_text
    ubi = next(yaml.safe_load_all(render((ROOT / "deploy/caravan/35-ojoseph-test-agent.yaml").read_text(encoding="utf-8"))))
    ubi_container = ubi["spec"]["template"]["spec"]["containers"][0]
    assert ubi["metadata"]["name"] == "ojoseph-test-agent"
    assert ubi_container["image"] == "image-registry.example/openshift/python:3.12-ubi9"
    assert ubi_container["env"][-1]["valueFrom"]["secretKeyRef"] == agent_key
    assert "deploy/caravan/35-ojoseph-test-agent.yaml" in (ROOT / "deploy.sh").read_text(encoding="utf-8")


def test_servicemonitor_matches_exporter_service() -> None:
    service = next(yaml.safe_load_all(render((ROOT / "deploy/phase2/40-service.yaml").read_text(encoding="utf-8"))))
    monitor = next(yaml.safe_load_all(render((ROOT / "deploy/phase2/50-servicemonitor.yaml").read_text(encoding="utf-8"))))
    labels = service["metadata"]["labels"]
    match = monitor["spec"]["selector"]["matchLabels"]
    assert match
    assert match.items() <= labels.items()
    assert monitor["spec"]["namespaceSelector"]["matchNames"] == [service["metadata"]["namespace"]]
    assert monitor["spec"]["endpoints"][0]["port"] == "metrics"
    assert monitor["spec"]["endpoints"][0]["honorLabels"] is True
    assert any(port["name"] == "metrics" for port in service["spec"]["ports"])


def test_vllm_usage_flag_script_sets_both_flags() -> None:
    script = (ROOT / "scripts/enable-vllm-usage-flags.sh").read_text(encoding="utf-8")
    assert "--enable-force-include-usage" in script
    assert "--enable-prompt-tokens-details" in script
    assert "enable-vllm-usage-flags.sh" not in (ROOT / "deploy.sh").read_text(encoding="utf-8")


def test_pricing_ui_can_reach_apiserver() -> None:
    policy = next(yaml.safe_load_all(render((ROOT / "deploy/phase4/60-networkpolicy.yaml").read_text(encoding="utf-8"))))
    ports = {port["port"] for rule in policy["spec"]["egress"] for port in rule["ports"]}
    assert 6443 in ports


def test_ui_serviceaccount_can_list_maas_models() -> None:
    docs = list(yaml.safe_load_all(render((ROOT / "deploy/phase1/40-model-reader.yaml").read_text(encoding="utf-8"))))
    role = next(doc for doc in docs if doc["kind"] == "ClusterRole")
    binding = next(doc for doc in docs if doc["kind"] == "ClusterRoleBinding")
    rule = role["rules"][0]
    assert role["metadata"]["name"] == "maas-tokenomics-model-reader"
    assert rule["apiGroups"] == ["maas.opendatahub.io"]
    assert rule["resources"] == ["maasmodelrefs"]
    assert rule["verbs"] == ["get", "list"]
    assert binding["roleRef"]["name"] == "maas-tokenomics-model-reader"
    assert binding["subjects"][0]["name"] == "maas-pricing-ui"
    assert binding["subjects"][0]["namespace"] == "maas-finops-ui"
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "maas-tokenomics-model-reader" in script


def test_ui_serviceaccount_can_view_thanos() -> None:
    binding = next(
        doc
        for doc in yaml.safe_load_all(render((ROOT / "deploy/phase1/20-monitoring-rbac.yaml").read_text(encoding="utf-8")))
        if doc["kind"] == "ClusterRoleBinding"
    )
    assert binding["roleRef"]["name"] == "cluster-monitoring-view"
    subject = binding["subjects"][0]
    assert subject["name"] == "maas-pricing-ui"
    assert subject["namespace"] == "maas-finops-ui"


def test_dashboard_configmap_labels_are_applied_by_deploy() -> None:
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    for label in (
        "console.openshift.io/dashboard=true",
        "console.openshift.io/odc-dashboard=true",
        "dashboard=true",
        "app.kubernetes.io/part-of=rhods-dashboard",
    ):
        assert label in script
    assert "openshift-config-managed" in script


def test_deploy_script_renders_without_placeholders() -> None:
    import subprocess

    completed = subprocess.run(
        ["bash", str(ROOT / "deploy.sh"), "render"],
        check=True,
        capture_output=True,
        text=True,
    )
    for token in PLACEHOLDERS:
        assert token not in completed.stdout
    assert "kind: Namespace" in completed.stdout
    assert "kind: ServiceMonitor" in completed.stdout
    assert "kind: Route" in completed.stdout
    assert "kind: MCPServer" in completed.stdout


def test_phase5_enables_catalogs_and_the_mcp_operator() -> None:
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "phases=(1 2 3 4)" in script
    dashboard = next(
        yaml.safe_load_all((ROOT / "deploy/caravan/80-dashboard-catalog-patch.yaml").read_text(encoding="utf-8"))
    )
    flags = dashboard["spec"]["dashboardConfig"]
    assert flags["agentsCatalog"] is True
    assert flags["mcpCatalog"] is True
    dsc = next(yaml.safe_load_all((ROOT / "deploy/caravan/81-dsc-mcp-operator-patch.yaml").read_text(encoding="utf-8")))
    components = dsc["spec"]["components"]
    assert list(components) == ["mcplifecycleoperator"]
    assert components["mcplifecycleoperator"]["managementState"] == "Managed"
    assert "80-dashboard-catalog-patch.yaml" in script
    assert "81-dsc-mcp-operator-patch.yaml" in script
    assert "mcplifecycleoperator" in (ROOT / "MAAS_MODIFICATIONS.md").read_text(encoding="utf-8")


def test_research_catalog_and_agent_manifests() -> None:
    mcp = yaml.safe_load((ROOT / "catalog/research-mcp.yaml").read_text(encoding="utf-8"))
    names = {item["name"] for item in mcp["mcp_servers"]}
    assert names == {"fetch", "wikipedia", "arxiv"}
    uris = {artifact["uri"] for item in mcp["mcp_servers"] for artifact in item["artifacts"]}
    assert "oci://ghcr.io/stackloklabs/gofetch/server:latest" in uris
    assert "oci://ghcr.io/cyanheads/wikipedia-mcp-server:latest" in uris
    assert "oci://ghcr.io/cyanheads/arxiv-mcp-server:latest" in uris
    sources = yaml.safe_load((ROOT / "catalog/mcp-sources.yaml").read_text(encoding="utf-8"))
    assert sources["mcp_catalogs"][0]["enabled"] is True
    assert sources["mcp_catalogs"][0]["properties"]["yamlCatalogPath"] == "research-mcp.yaml"
    agent_sources = yaml.safe_load((ROOT / "catalog/agent-sources.yaml").read_text(encoding="utf-8"))
    assert agent_sources["agent_catalogs"][0]["enabled"] is True
    agent = yaml.safe_load((ROOT / "catalog/researcher-agent.yaml").read_text(encoding="utf-8"))
    assert agent["agents"][0]["displayName"] == "researcher agent"
    assert agent["agents"][0]["framework"] == "python"
    servers = list(
        yaml.safe_load_all(render((ROOT / "deploy/caravan/37-research-mcp.yaml").read_text(encoding="utf-8")))
    )
    assert [item["metadata"]["name"] for item in servers] == ["fetch-mcp", "wikipedia-mcp", "arxiv-mcp"]
    assert all(item["spec"]["config"]["port"] == 8080 for item in servers)
    researcher = next(
        yaml.safe_load_all(render((ROOT / "deploy/caravan/36-researcher-agent.yaml").read_text(encoding="utf-8")))
    )
    assert researcher["metadata"]["annotations"]["openshift.io/display-name"] == "researcher agent"
    container = researcher["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "registry.example/researcher-agent:0.1.0"
    assert researcher["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] == "agent"
    key = next(item for item in container["env"] if item["name"] == "OPENAI_API_KEY")["valueFrom"]["secretKeyRef"]
    assert key == {"name": "ojoseph-test-agent-secret", "key": "OPENAI_API_KEY"}
    ingest = next(item for item in container["env"] if item["name"] == "INGEST_URL")
    assert ingest["value"] == "http://maas-tokenomics-exporter.maas-finops.svc.cluster.local:8080/v1/usage"
    policies = list(yaml.safe_load_all(render((ROOT / "deploy/caravan/70-networkpolicy.yaml").read_text(encoding="utf-8"))))
    policy = policies[0]
    webui_policy = next(item for item in policies if item["metadata"]["name"] == "open-webui")
    webui_ports = {port["port"] for rule in webui_policy["spec"]["egress"] for port in rule["ports"]}
    assert 8080 in webui_ports
    assert policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["app"] == "open-webui"
    ports = {port["port"] for rule in policy["spec"]["egress"] for port in rule["ports"]}
    assert {53, 443, 6443, 8080} <= ports
    source = (ROOT / "researcher/app.py").read_text(encoding="utf-8")
    assert "/healthz" in source
    assert "/.well-known/agent-card.json" in source
    assert "/v1/chat/completions" in source
    assert '"researcher agent"' in source
    assert "OPENAI_API_KEY=" not in (ROOT / "deploy/caravan/36-researcher-agent.yaml").read_text(encoding="utf-8")
