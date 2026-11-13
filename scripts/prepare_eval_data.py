"""Import the bundled course knowledge and verify the fixed evaluation suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from uuid import UUID


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = PROJECT_ROOT / "docs" / "datas" / "evals" / "rag_cases.json"
KNOWLEDGE_DIR = PROJECT_ROOT / "docs" / "datas" / "knowledge_base"
DEMO_FULL_NAME = "course-demo/devflow-eval-course"


def load_dataset(suite_path: Path = SUITE_PATH, knowledge_dir: Path = KNOWLEDGE_DIR) -> dict:
    raw = suite_path.read_bytes()
    suite = json.loads(raw.decode("utf-8"))
    if not isinstance(suite, dict) or not suite.get("name") or not suite.get("cases"):
        raise ValueError("The suite must contain a name and a non-empty cases list.")
    ids: set[str] = set()
    files: dict[str, Path] = {}
    for case in suite["cases"]:
        case_id = case.get("id")
        if not case_id or case_id in ids:
            raise ValueError(f"Missing or duplicate case ID: {case_id}")
        ids.add(case_id)
        if not case.get("question") or not case.get("reference"):
            raise ValueError(f"Missing question or reference: {case_id}")
        name = case.get("source_file")
        if not name or Path(name).name != name or "\\" in name:
            raise ValueError(f"source_file must be a filename: {case_id}")
        path = (knowledge_dir / name).resolve()
        if path.parent != knowledge_dir.resolve() or not path.is_file():
            raise ValueError(f"Missing knowledge document: {name}")
        if not path.read_text(encoding="utf-8").strip():
            raise ValueError(f"Empty knowledge document: {name}")
        files[name] = path
        reference = case["reference"]
        if any(value.casefold() not in reference.casefold() for value in case.get("must_include", [])):
            raise ValueError(f"Reference violates must_include: {case_id}")
        if any(value.casefold() in reference.casefold() for value in case.get("must_not_include", [])):
            raise ValueError(f"Reference violates must_not_include: {case_id}")
        if case.get("answer_regex") and re.fullmatch(case["answer_regex"], reference) is None:
            raise ValueError(f"Reference violates answer_regex: {case_id}")
    return {"suite": suite, "sha256": hashlib.sha256(raw).hexdigest(), "files": files}


def response_json(response):
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"API returned HTTP {response.status_code}: {response.text[:800]}")
    return response.json()


def prepare_dataset(client, dataset: dict, *, repo_id: str | None, demo_repo: bool) -> dict:
    if demo_repo:
        repos = response_json(client.get("/api/repos"))
        existing = next((row for row in repos if row["full_name"] == DEMO_FULL_NAME), None)
        if existing:
            repo_id = str(existing["id"])
        else:
            created = response_json(client.post("/api/repos/connect", json={
                "owner": "course-demo", "repo": "devflow-eval-course", "demo_mode": True,
            }))
            repo_id = str(created["repo_id"])
    if not repo_id:
        raise ValueError("Select --repo-id or --demo-repo.")
    UUID(repo_id)
    existing = response_json(client.get(f"/api/repos/{repo_id}/knowledge"))
    # Refuse ambiguous source versions instead of silently mixing old and new facts.
    for name, path in dataset["files"].items():
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        conflicts = [row for row in existing if (row.get("metadata") or {}).get("filename") == name
                     and (row.get("metadata") or {}).get("checksum") != checksum]
        if conflicts:
            raise RuntimeError(f"A different version of {name} is already indexed. Use a clean course repository or remove the old knowledge explicitly.")
    uploads = []
    for name, path in dataset["files"].items():
        with path.open("rb") as stream:
            uploaded = response_json(client.post(
                f"/api/repos/{repo_id}/knowledge/upload",
                files={"file": (name, stream, "text/markdown")},
            ))
        if not uploaded.get("indexed") or not uploaded.get("chunks"):
            raise RuntimeError(f"Knowledge was not indexed: {name}")
        uploads.append(uploaded)
    suites = response_json(client.get("/api/evals/suites", params={"repo_id": repo_id}))
    suite_id = dataset["suite"]["name"]
    selected = next((row for row in suites if row["id"] == suite_id), None)
    if selected is None:
        raise RuntimeError("The backend cannot find the bundled suite. Restart the backend from this extracted project.")
    if selected.get("sha256") != dataset["sha256"]:
        raise RuntimeError("Backend suite SHA-256 differs from this package. Restart the matching backend; do not compare different suite versions.")
    if not selected.get("ready"):
        raise RuntimeError(f"Missing indexed sources: {selected.get('missing_sources')}")
    return {"repo_id": repo_id, "evaluation_set": selected, "uploads": uploads}


def run_evaluation(client, prepared: dict, *, run_judge: bool, wait_seconds: int) -> dict:
    queued = response_json(client.post("/api/evals/rag/run", json={
        "name": "course-fixed-rag-eval", "repo_id": prepared["repo_id"],
        "suite_id": prepared["evaluation_set"]["id"], "top_k": 5, "run_judge": run_judge,
    }))
    eval_id = queued["eval_id"]
    print(f"Eval ID: {eval_id}", flush=True)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        current = response_json(client.get(f"/api/evals/{eval_id}"))
        if current["status"] not in {"queued", "running"}:
            return current
        time.sleep(1)
    raise TimeoutError(f"Eval {eval_id} is still running. Query GET /api/evals/{eval_id}; do not resubmit blindly.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--check", action="store_true", help="Validate bundled files without a server or API key.")
    target.add_argument("--repo-id", type=UUID, help="Import into an existing repository UUID.")
    target.add_argument("--demo-repo", action="store_true", help="Create/reuse a local course repository record without GitHub calls.")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--run", action="store_true", help="Submit the fixed suite and wait for a terminal result; model calls may incur costs.")
    parser.add_argument("--without-judge", action="store_true", help="Only check the execution path, never claim full RAGAS quality passed.")
    parser.add_argument("--wait-seconds", type=int, default=1800)
    parser.add_argument("--output", type=Path, help="Save the preparation or evaluation result as JSON.")
    args = parser.parse_args(argv)
    if args.wait_seconds < 1:
        parser.error("--wait-seconds must be positive")
    if args.check and (args.run or args.without_judge):
        parser.error("--check cannot run an evaluation")
    if args.without_judge and not args.run:
        parser.error("--without-judge requires --run")
    try:
        dataset = load_dataset()
        print(f"Dataset OK: {len(dataset['suite']['cases'])} cases, {len(dataset['files'])} knowledge files, SHA-256 {dataset['sha256']}")
        if args.check:
            return 0
        import httpx

        with httpx.Client(base_url=args.api_base_url.rstrip("/"), timeout=120, trust_env=False) as client:
            prepared = prepare_dataset(client, dataset, repo_id=str(args.repo_id) if args.repo_id else None, demo_repo=args.demo_repo)
            print(f"Repository: {prepared['repo_id']}; suite ready=true", flush=True)
            result = run_evaluation(client, prepared, run_judge=not args.without_judge, wait_seconds=args.wait_seconds) if args.run else prepared
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not args.run:
            return 0
        detail = result["result"]
        print(json.dumps({"eval_id": result["eval_id"], "status": result["status"],
                          "quality_status": detail.get("quality_status"),
                          "evaluation_complete": detail.get("evaluation_complete"),
                          "passed": detail.get("passed"), "hard_gate": detail.get("hard_gate")}, ensure_ascii=False, indent=2))
        if detail.get("evaluation_complete") and detail.get("passed"):
            return 0
        print("Execution returned a result, but full RAGAS quality did NOT pass. Inspect the saved report or the /evals page.")
        return 2
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
