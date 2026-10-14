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
    "__GRAFANA_LABEL__": "grafana",
    "__PERSES_DATASOURCE__": "data-science-prometheus-datasource",
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
    assert all(isinstance(doc, dict) and doc.get("kind") for doc in documents)


def test_grafana_dashboard_contract() -> None:
    dashboard = json.loads((ROOT / "deploy/phase3/tokenomics.json").read_text(encoding="utf-8"))
    assert dashboard["title"] == "Tokenomics"
    assert dashboard["uid"] == "maas-tokenomics"
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert {"Input tokens", "Cache tokens", "Decoded tokens", "Tokens by user and type"} <= titles
    ranges = {item["display"] for item in dashboard["timepicker"]["quick_ranges"]}
    assert ranges == {"Last 1 hour", "Last 24 hours", "Last 7 days", "Last 30 days"}
    joined = json.dumps(dashboard)
    assert "maas_user_token_usage_total" in joined
    assert 'token_type=\\"input\\"' in joined or 'token_type="input"' in joined


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
