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
    "__OPENAI_BASE_URL__": "https://maas.example/v1",
    "__OPENAI_MODEL__": "example-model",
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


def test_images_are_built_from_ubi_containerfiles() -> None:
    containerfiles = [
        ROOT / "exporter/Containerfile",
        ROOT / "ui/Containerfile",
        ROOT / "grafana/Containerfile",
        ROOT / "openwebui/Containerfile",
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
    persist = next(item for item in containers["open-webui"]["env"] if item["name"] == "ENABLE_PERSISTENT_CONFIG")
    assert persist["value"] == "false"
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
