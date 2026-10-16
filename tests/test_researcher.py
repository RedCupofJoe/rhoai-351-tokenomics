import importlib.util
import json
from pathlib import Path

from researcher.app import agent_card, decode_mcp_body, usage_from_completion


def test_agent_card_uses_the_console_name() -> None:
    card = agent_card()
    assert card["name"] == "researcher agent"
    assert {skill["id"] for skill in card["skills"]} == {"fetch", "wikipedia", "arxiv"}


def test_decode_mcp_body_reads_json_and_sse() -> None:
    assert decode_mcp_body("") is None
    assert decode_mcp_body('{"jsonrpc":"2.0","id":1,"result":{}}')["id"] == 1
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n'
    decoded = decode_mcp_body(body, "text/event-stream")
    assert decoded["id"] == 2


def test_open_webui_chat_selects_the_researcher_and_records_usage(monkeypatch) -> None:
    from researcher.app import (
        CHAT_MODEL_ID,
        answer_chat,
        chat_user,
        completion_document,
        ingest_event,
        messages_query,
        openai_model_list,
    )

    assert openai_model_list()["data"][0]["id"] == CHAT_MODEL_ID
    assert openai_model_list()["data"][0]["name"] == "researcher agent"
    document = {
        "model": CHAT_MODEL_ID,
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": [{"type": "text", "text": "What is a token?"}]},
        ],
    }
    assert messages_query(document) == "What is a token?"
    assert chat_user({"X-OpenWebUI-User-Name": "nora"}, document) == "nora"
    assert chat_user({}, {}) == "open-webui"

    def fake_research(query, page_url="", user="", searches=None, urls=None, articles=None, papers=None):
        assert query == "What is a token?"
        assert user == "nora"
        return {
            "answer": "A token is a piece of text.",
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "total_tokens": 16,
                "cached_tokens": 1,
            },
        }

    monkeypatch.setattr("researcher.app.research", fake_research)
    posted = {}

    def fake_post(user, usage, query):
        posted["event"] = ingest_event(user, usage, query)

    monkeypatch.setattr("researcher.app.post_ingest", fake_post)
    status, content_type, body = answer_chat(document, {"X-OpenWebUI-User-Name": "nora"})
    assert status == 200
    assert content_type == "application/json"
    payload = json.loads(body)
    assert payload["model"] == CHAT_MODEL_ID
    assert payload["choices"][0]["message"]["content"] == "A token is a piece of text."
    assert payload["usage"]["prompt_tokens_details"]["cached_tokens"] == 1
    assert posted["event"]["user"] == "nora"
    assert posted["event"]["model"] == CHAT_MODEL_ID
    assert posted["event"]["namespace"] == "test-project"
    assert posted["event"]["cached_tokens"] == 1
    streamed = completion_document("hello", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cached_tokens": 0})
    assert streamed["choices"][0]["finish_reason"] == "stop"


def test_complete_asks_for_a_final_answer_and_reads_reasoning(monkeypatch) -> None:
    from researcher.app import complete

    captured = {}

    class Response:
        def __init__(self, document: dict):
            self.document = document

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return json.dumps(self.document).encode("utf-8")

    def urlopen(request, timeout=0):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        if captured.get("blank"):
            document = {
                "choices": [{"message": {"content": "  ", "reasoning": "A pod groups containers."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }
        else:
            document = {
                "choices": [{"message": {"content": "A pod groups containers.", "reasoning": "think"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "total_tokens": 7,
                    "prompt_tokens_details": {"cached_tokens": 1},
                },
            }
        return Response(document)

    monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt")
    monkeypatch.setattr("researcher.app.urllib.request.urlopen", urlopen)
    text, usage = complete("What is a pod?")
    assert captured["body"]["messages"][0]["role"] == "system"
    assert "Do not call tools" in captured["body"]["messages"][0]["content"]
    assert captured["body"]["messages"][1] == {"role": "user", "content": "What is a pod?"}
    assert text == "A pod groups containers."
    assert usage["cached_tokens"] == 1
    captured["blank"] = True
    text, _usage = complete("What is a pod?")
    assert text == "A pod groups containers."


def test_usage_from_completion_keeps_cache_tokens() -> None:
    usage = usage_from_completion(
        {
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 40,
                "total_tokens": 160,
                "prompt_tokens_details": {"cached_tokens": 15},
            }
        }
    )
    assert usage == {
        "prompt_tokens": 120,
        "completion_tokens": 40,
        "total_tokens": 160,
        "cached_tokens": 15,
    }
    assert usage_from_completion({})["prompt_tokens"] == 0


def test_research_demo_records_four_users_through_mcp_and_ingest() -> None:
    path = Path(__file__).resolve().parents[1] / "scripts/impersonate_research_users.py"
    spec = importlib.util.spec_from_file_location("impersonate_research_users", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = [person["user"] for person in module.USERS]
    assert names == ["nora", "owen", "priya", "samir"]
    queries = [person["query"] for person in module.USERS]
    assert len(set(queries)) == 4
    assert all(len(person["query"]) >= 500 for person in module.USERS)
    assert all(len(person["searches"]) >= 2 for person in module.USERS)
    assert all(person["urls"][0].startswith("https://") for person in module.USERS)
    agent = Path(__file__).resolve().parents[1].joinpath("researcher/app.py").read_text(encoding="utf-8")
    assert "wikipedia_get_summary" in agent
    assert "arxiv_get_metadata" in agent
    event = module.usage_event(
        "nora",
        "publishers/llm/models/gpt-oss-20b",
        {"prompt_tokens": 10, "completion_tokens": 4, "cached_tokens": 2},
        module.USERS[0]["query"],
    )
    assert event["user"] == "nora"
    assert event["namespace"] == "test-project"
    assert event["cached_tokens"] == 2
    assert event["id"] == module.event_id("nora", module.USERS[0]["query"])
    source = path.read_text(encoding="utf-8")
    assert "/research" in source
    assert "/v1/usage" in source
    assert "--as=" in source
    assert "oc get user" in source
    record = source.split("def record_usage", 1)[1].split("def main", 1)[0]
    assert "--as" not in record
    creator = (Path(__file__).resolve().parents[1] / "scripts/create_htpasswd_users.sh").read_text(encoding="utf-8")
    assert "tokenomics-htpasswd" in creator
    assert "Using the existing authentication providers:" in creator
    assert creator.index("Using the existing authentication providers:") < creator.index("openshift-config create secret")
    deploy = (Path(__file__).resolve().parents[1] / "deploy.sh").read_text(encoding="utf-8")
    assert "create_htpasswd_users.sh" not in deploy
    rbac = (Path(__file__).resolve().parents[1] / "deploy/caravan/82-research-user-rbac.yaml").read_text(encoding="utf-8")
    assert "tokenomics-researcher" in rbac
    assert "pods/exec" in rbac
    assert "secrets" not in rbac
    assert "maas-finops-ui" not in rbac
