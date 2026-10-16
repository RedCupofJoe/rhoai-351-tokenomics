#!/usr/bin/env python3
"""Run one deep research question as each demo user and record the tokens.

Each question is a separate brief. The researcher agent searches Wikipedia
and arXiv, reads the named articles and papers, fetches the source pages,
and then answers through the MaaS gateway. The research exec is authorized
with oc --as that OpenShift user. The gateway usage object is posted to the
tokenomics exporter under the name returned by oc get user. Each run records
a new event.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

USERS = (
    {
        "user": "nora",
        "query": (
            "Compare the original retrieval-augmented generation design in "
            "Lewis et al. 2020 with a production assistant that uses a dense "
            "passage index. What did the paper claim about parametric memory "
            "versus a non-parametric index? Which tasks, datasets, and metrics "
            "did the experiments use, and what did those results show about "
            "grounding an answer in retrieved text? Which of those claims still "
            "follow when the retriever returns passages instead of documents "
            "and the generator is an instruction-tuned model? Separate what the "
            "paper measured from what later systems assume, and name the claims "
            "the sources do not establish."
        ),
        "searches": [
            "retrieval-augmented generation Lewis 2020",
            "dense passage retrieval question answering",
        ],
        "articles": ["Retrieval-augmented generation"],
        "papers": ["2005.11401"],
        "urls": [
            "https://arxiv.org/abs/2005.11401",
            "https://en.wikipedia.org/wiki/Retrieval-augmented_generation",
        ],
    },
    {
        "user": "owen",
        "query": (
            "Explain how the Kubernetes scheduler places a pod when several "
            "nodes look usable. Cover the difference between filtering and "
            "scoring, how CPU and memory requests change which nodes remain "
            "feasible, and what happens when limits differ from requests. Then "
            "explain priority and preemption when the cluster is full: which "
            "pods can be preempted, what PodDisruptionBudget and "
            "terminationGracePeriodSeconds change, and how node affinity or a "
            "topology spread constraint can reject a node that otherwise scored "
            "higher. Distinguish behavior the scheduler documentation specifies "
            "from behavior that depends on the scoring plugins in use."
        ),
        "searches": [
            "Kubernetes scheduler filtering scoring",
            "Kubernetes pod priority preemption",
        ],
        "articles": ["Kubernetes"],
        "papers": [],
        "urls": [
            "https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/",
            "https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/",
        ],
    },
    {
        "user": "priya",
        "query": (
            "Reconstruct the argument of Vaswani et al., Attention Is All You "
            "Need, 2017. Why did scaled dot-product attention and multi-head "
            "attention replace recurrence and convolution for sequence "
            "transduction? What do the encoder stack and the decoder stack "
            "each compute, including residual connections and layer "
            "normalization? How are token positions represented without "
            "recurrence? Which translation results in the paper support the "
            "claim that attention is sufficient, and which comparisons are "
            "against recurrent or convolutional baselines? Keep the paper's "
            "evidence separate from later practice such as decoder-only "
            "language models, and say what this paper does not demonstrate."
        ),
        "searches": [
            "Attention Is All You Need Vaswani",
            "transformer multi-head attention positional encoding",
        ],
        "articles": ["Transformer (deep learning architecture)"],
        "papers": ["1706.03762"],
        "urls": [
            "https://arxiv.org/abs/1706.03762",
            "https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)",
        ],
    },
    {
        "user": "samir",
        "query": (
            "What does Red Hat OpenShift add on top of upstream Kubernetes for "
            "building and operating applications? Compare the cluster control "
            "plane, the role of Operators in installing and updating platform "
            "services, and how an application is built and rolled out on "
            "OpenShift versus a pipeline that only produces a container image "
            "for upstream Kubernetes. Explain which of those pieces are "
            "OpenShift platform features and which already exist in Kubernetes, "
            "including projects, routes or ingress, and built-in image builds. "
            "Use the product overview and the architecture documentation, and "
            "state which differences the sources actually describe."
        ),
        "searches": [
            "Red Hat OpenShift Kubernetes",
            "OpenShift architecture operators routes",
        ],
        "articles": ["Red Hat OpenShift"],
        "papers": [],
        "urls": [
            "https://www.redhat.com/en/technologies/cloud-computing/openshift",
            "https://docs.openshift.com/container-platform/latest/architecture/architecture.html",
        ],
    },
)


def event_id(user: str, query: str, run_id: str = "") -> str:
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
    suffix = "-" + run_id if run_id else ""
    return "research-" + user + "-" + digest + suffix


def usage_event(user: str, model: str, usage: dict, query: str, run_id: str = "") -> dict:
    return {
        "id": event_id(user, query, run_id),
        "user": user,
        "model": model or "unknown",
        "namespace": os.environ.get("RESEARCH_NAMESPACE", "test-project"),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "cached_tokens": int(usage.get("cached_tokens") or 0),
    }


def openshift_user(name: str) -> str:
    """Confirm oc get user <name> exists before any research."""
    completed = subprocess.run(
        ["oc", "get", "user", name, "-o", "jsonpath={.metadata.name}"],
        text=True,
        capture_output=True,
        check=False,
    )
    found = (completed.stdout or "").strip()
    if completed.returncode != 0 or found != name:
        detail = (completed.stderr or "missing").strip()
        raise RuntimeError("oc get user " + name + " failed: " + detail[-200:])
    return found


def _oc_python(
    namespace: str,
    deployment: str,
    container: str,
    code: str,
    payload: str,
    as_user: str = "",
) -> str:
    command = ["oc"]
    if as_user:
        command.extend(["--as=" + as_user, "--as-group=system:authenticated"])
    command.extend([
        "exec",
        "-i",
        "-n",
        namespace,
        "deploy/" + deployment,
    ])
    if container:
        command.extend(["-c", container])
    command.extend(["--", "python", "-c", code])
    completed = subprocess.run(
        command,
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "oc exec failed").strip()
        raise RuntimeError(detail[-500:])
    return completed.stdout


_RESEARCH_CODE = """
import json, sys, urllib.request
raw = sys.stdin.read()
request = urllib.request.Request(
    "http://127.0.0.1:8080/research",
    data=raw.encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=300) as response:
    sys.stdout.write(response.read().decode("utf-8"))
"""

_INGEST_CODE = """
import json, os, sys, urllib.error, urllib.request
raw = sys.stdin.read()
token = os.environ.get("INGEST_TOKEN", "")
request = urllib.request.Request(
    "http://127.0.0.1:8080/v1/usage",
    data=raw.encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        sys.stdout.write(response.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", "replace")
    if token:
        body = body.replace(token, "[redacted]")
    sys.stderr.write("HTTP %s %s" % (exc.code, body[:300]))
    sys.exit(1)
"""


def research_as(user: dict, namespace: str, as_user: str) -> dict:
    body = json.dumps(
        {
            "query": user["query"],
            "searches": user["searches"],
            "urls": user["urls"],
            "articles": user["articles"],
            "papers": user["papers"],
            "user": as_user,
        }
    )
    raw = _oc_python(namespace, "researcher-agent", "", _RESEARCH_CODE, body, as_user)
    return json.loads(raw)


def record_usage(event: dict, namespace: str) -> dict:
    raw = _oc_python(namespace, "maas-tokenomics-exporter", "exporter", _INGEST_CODE, json.dumps(event))
    return json.loads(raw)


def main() -> int:
    research_namespace = os.environ.get("RESEARCH_NAMESPACE", "test-project")
    exporter_namespace = os.environ.get("EXPORTER_NAMESPACE", "maas-finops-ui")
    run_id = str(int(time.time()))
    recorded = 0
    for person in USERS:
        print("Researching as " + person["user"], flush=True)
        try:
            verified = openshift_user(person["user"])
            report = research_as(person, research_namespace, verified)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print("  research failed: " + str(exc)[:300], flush=True)
            continue
        usage = report.get("usage") or {}
        spent = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        if spent <= 0:
            print("  no gateway usage: " + str(report.get("answerError") or "empty usage"), flush=True)
            continue
        event = usage_event(verified, str(report.get("model") or ""), usage, person["query"], run_id)
        try:
            result = record_usage(event, exporter_namespace)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print("  ingest failed: " + str(exc)[:300], flush=True)
            continue
        recorded += 1
        print(
            "  input=%s cache=%s decoded=%s accepted=%s duplicates=%s"
            % (
                event["prompt_tokens"],
                event["cached_tokens"],
                event["completion_tokens"],
                result.get("accepted"),
                result.get("duplicates"),
            ),
            flush=True,
        )
    print("Recorded %s of %s users." % (recorded, len(USERS)), flush=True)
    return 0 if recorded else 1


if __name__ == "__main__":
    sys.exit(main())
