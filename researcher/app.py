"""Researcher agent. Serves an agent card and calls MCP plus the MaaS gateway."""

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")


def decode_mcp_body(body: str, content_type: str = "") -> dict | None:
    """Return the JSON-RPC object from a JSON body or an SSE stream."""
    text = (body or "").strip()
    if not text:
        return None
    streamed = "text/event-stream" in content_type or text.startswith(("event:", "data:"))
    if streamed:
        chosen = None
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            chosen = json.loads(payload)
        return chosen
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("MCP response was not an object")
    return parsed


def agent_card() -> dict:
    return {
        "name": "researcher agent",
        "description": (
            "Looks up web pages, Wikipedia, and arXiv through MCP, "
            "then answers through the MaaS gateway."
        ),
        "version": "0.1.0",
        "protocolVersion": "0.3.0",
        "url": os.environ.get("AGENT_URL", "http://researcher-agent:8080"),
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "fetch",
                "name": "Web fetch",
                "description": "Fetch a public URL and extract its text.",
            },
            {
                "id": "wikipedia",
                "name": "Wikipedia",
                "description": "Search and read Wikipedia articles.",
            },
            {
                "id": "arxiv",
                "name": "arXiv",
                "description": "Search arXiv papers and read their metadata.",
            },
        ],
    }


def mcp_servers() -> dict[str, str]:
    return {
        "fetch": os.environ.get("MCP_FETCH_URL", ""),
        "wikipedia": os.environ.get("MCP_WIKIPEDIA_URL", ""),
        "arxiv": os.environ.get("MCP_ARXIV_URL", ""),
    }


class MCPError(RuntimeError):
    pass


def _post_mcp(url: str, payload: dict, session: str | None, protocol: str, timeout: int = 25):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": protocol,
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            content_type = response.headers.get("Content-Type", "")
            session_id = response.headers.get("Mcp-Session-Id") or session
            return session_id, decode_mcp_body(raw, content_type)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise MCPError(f"HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise MCPError(f"unreachable: {exc.reason}") from None


class MCPSession:
    def __init__(self, url: str):
        self.url = url
        self.session = None
        self.protocol = PROTOCOL_VERSIONS[0]
        self._next_id = 1

    def _call(self, method: str, params: dict | None, notify: bool = False, timeout: int = 25):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            message["id"] = self._next_id
            self._next_id += 1
        self.session, body = _post_mcp(self.url, message, self.session, self.protocol, timeout)
        if body and body.get("error"):
            raise MCPError(json.dumps(body["error"])[:500])
        return body

    def start(self) -> None:
        last_error = None
        for protocol in PROTOCOL_VERSIONS:
            self.protocol = protocol
            try:
                self._call(
                    "initialize",
                    {
                        "protocolVersion": protocol,
                        "capabilities": {},
                        "clientInfo": {"name": "researcher-agent", "version": "0.1.0"},
                    },
                )
            except (MCPError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                continue
            self._call("notifications/initialized", None, notify=True)
            return
        raise MCPError(str(last_error or "initialize failed"))

    def tool_names(self) -> list[str]:
        body = self._call("tools/list", {})
        tools = ((body or {}).get("result") or {}).get("tools") or []
        return [item.get("name", "") for item in tools if isinstance(item, dict) and item.get("name")]

    def call_tool(self, name: str, arguments: dict) -> str:
        body = self._call("tools/call", {"name": name, "arguments": arguments}, timeout=60)
        content = ((body or {}).get("result") or {}).get("content") or []
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)[:1500]


def usage_from_completion(document: dict) -> dict:
    """Keep the gateway usage object in a stable shape for showback."""
    usage = document.get("usage") if isinstance(document, dict) else None
    if not isinstance(usage, dict):
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
        }
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or 0),
    }


def complete(prompt: str) -> tuple[str, dict]:
    base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("OPENAI_MODEL", "")
    if not base or not key or not model:
        raise MCPError("MaaS gateway settings are incomplete")
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 900,
            }
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            document = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MCPError(f"MaaS HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise MCPError(f"MaaS unreachable: {exc.reason}") from None
    choices = document.get("choices") or []
    text = ""
    if choices:
        text = ((choices[0].get("message") or {}).get("content") or "")[:2000]
    return text, usage_from_completion(document)


def _clip(text: str, limit: int = 1000) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit]


def _safe_tool(session: MCPSession, name: str, arguments: dict) -> str:
    try:
        return _clip(session.call_tool(name, arguments))
    except (MCPError, json.JSONDecodeError, ValueError, TimeoutError) as exc:
        return "error: " + str(exc)[:300]


def _use_server(name: str, url: str, query: str, page_url: str = "") -> dict:
    if not url:
        return {"error": "URL is unset"}
    session = MCPSession(url)
    session.start()
    names = session.tool_names()
    result = {"tools": names}
    if name == "wikipedia" and "wikipedia_search_articles" in names:
        result["search"] = session.call_tool(
            "wikipedia_search_articles", {"query": query, "limit": 3}
        )
    elif name == "arxiv" and "arxiv_search" in names:
        result["search"] = session.call_tool("arxiv_search", {"query": query, "max_results": 3})
    elif name == "fetch" and "fetch" in names:
        target = page_url if page_url.startswith(("http://", "https://")) else ""
        if not target and query.startswith(("http://", "https://")):
            target = query
        if target:
            result["search"] = session.call_tool("fetch", {"url": target, "max_length": 1500})
    return result


def _deep_server(
    name: str,
    url: str,
    terms: list[str],
    pages: list[str],
    titles: list[str],
    paper_ids: list[str],
) -> dict:
    if not url:
        return {"error": "URL is unset"}
    session = MCPSession(url)
    session.start()
    names = session.tool_names()
    notes: list[str] = []
    if name == "wikipedia":
        if "wikipedia_search_articles" in names:
            for term in terms[:3]:
                notes.append("search " + term + ": " + _safe_tool(
                    session, "wikipedia_search_articles", {"query": term, "limit": 3}
                ))
        if "wikipedia_get_summary" in names:
            for title in titles[:2]:
                notes.append("summary " + title + ": " + _safe_tool(
                    session, "wikipedia_get_summary", {"title": title}
                ))
    elif name == "arxiv":
        if "arxiv_search" in names:
            for term in terms[:3]:
                notes.append("search " + term + ": " + _safe_tool(
                    session, "arxiv_search", {"query": term, "max_results": 3}
                ))
        if paper_ids and "arxiv_get_metadata" in names:
            notes.append("metadata: " + _safe_tool(
                session, "arxiv_get_metadata", {"paper_ids": paper_ids[:2]}
            ))
    elif name == "fetch" and "fetch" in names:
        for page in pages[:3]:
            notes.append(page + ": " + _safe_tool(
                session, "fetch", {"url": page, "max_length": 1500}
            ))
    return {"tools": names, "search": "\n".join(note for note in notes if note)}


def research(
    query: str,
    page_url: str = "",
    user: str = "",
    searches: list[str] | None = None,
    urls: list[str] | None = None,
    articles: list[str] | None = None,
    papers: list[str] | None = None,
) -> dict:
    terms = [item.strip() for item in (searches or []) if item and item.strip()]
    pages = []
    for item in [page_url, *(urls or [])]:
        text = (item or "").strip()
        if text.startswith(("http://", "https://")) and text not in pages:
            pages.append(text)
    titles = [item.strip() for item in (articles or []) if item and item.strip()]
    paper_ids = [item.strip() for item in (papers or []) if item and item.strip()]
    findings = {}
    deep = bool(terms or len(pages) > 1 or titles or paper_ids)
    for name, url in mcp_servers().items():
        try:
            if deep:
                findings[name] = _deep_server(
                    name, url, terms or [query[:180]], pages, titles, paper_ids
                )
            else:
                findings[name] = _use_server(name, url, query, page_url)
        except (MCPError, json.JSONDecodeError, ValueError, TimeoutError) as exc:
            findings[name] = {"error": str(exc)[:500]}
    notes = []
    for name, finding in findings.items():
        text = finding.get("search") or finding.get("error") or ", ".join(finding.get("tools") or [])
        notes.append(f"{name}: {_clip(text, 1600)}")
    prompt = (
        "Answer every part of the question from these tool notes. "
        "Name the source for each part, and say what the sources do not establish.\n"
        + "\n".join(notes)
    )
    prompt = f"Question: {query}\n{prompt}"[:7500]
    answer = ""
    answer_error = ""
    usage = usage_from_completion({})
    try:
        answer, usage = complete(prompt)
    except MCPError as exc:
        answer_error = str(exc)
    report = {
        "query": query,
        "sources": findings,
        "answer": answer,
        "usage": usage,
        "model": os.environ.get("OPENAI_MODEL", ""),
    }
    if user:
        report["user"] = user
    if answer_error:
        report["answerError"] = answer_error
    return report


def _string_list(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"researcher-agent {self.address_string()} {fmt % args}", flush=True)

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        if path == "/.well-known/agent-card.json":
            body = json.dumps(agent_card()).encode("utf-8")
            self._send(200, body, "application/json")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/research":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            document = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"invalid json"}', "application/json")
            return
        query = str(document.get("query") or "retrieval augmented generation").strip()
        page_url = str(document.get("url") or "").strip()
        user = str(document.get("user") or "").strip()
        searches = _string_list(document.get("searches"))
        urls = _string_list(document.get("urls"))
        articles = _string_list(document.get("articles"))
        papers = _string_list(document.get("papers"))
        report = research(
            query[:2500],
            page_url[:500],
            user[:200],
            searches[:4],
            urls[:3],
            articles[:3],
            papers[:2],
        )
        self._send(200, json.dumps(report).encode("utf-8"), "application/json")


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(
        "researcher agent ready "
        f"model={os.environ.get('OPENAI_MODEL', '')} "
        f"base={os.environ.get('OPENAI_BASE_URL', '')} "
        f"key_set={bool(os.environ.get('OPENAI_API_KEY'))}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
