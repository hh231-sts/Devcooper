"""Version-bound materials. Archives are retained with the review, independent of checkout sync."""
import io
import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import PurePosixPath

from app.services.code_search import SECRET_FILENAMES, SKIP_DIRS, TEXT_EXTENSIONS, TEXT_FILENAMES

COMMIT_SHA_PATTERN = r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})"


def ci_relationship(pr, run) -> str:
    if not pr or not run or not pr.head_sha or not run.commit_sha:
        return "unknown"
    if not all(re.fullmatch(COMMIT_SHA_PATTERN, sha) for sha in (pr.head_sha, run.commit_sha)):
        return "unknown"
    if run.commit_sha == pr.head_sha:
        return "pr_head"
    for linked in run.pull_requests_json or []:
        if linked.get("number") == pr.number and (linked.get("head") or {}).get("sha") == pr.head_sha:
            if run.commit_sha == pr.merge_commit_sha:
                return "synthetic_merge"
    return "unrelated"


def content_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def search_materials(items, query):
    terms = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", query.lower())
    hits = []
    for item in items:
        text = f"{item.get('title', '')}\n{item.get('body') or item.get('content') or ''}"
        score = sum(text.lower().count(term) for term in terms)
        if score:
            hits.append({"id": item["id"], "number": item.get("number"), "title": item.get("title"),
                         "score": score / (score + 1), "content_digest": item["content_digest"], "preview": text[:240]})
    return sorted(hits, key=lambda item: (-item["score"], item["id"]))[:12]


def git_read(checkout: str, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", checkout, *args], capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def require_commit_sha(sha):
    if not re.fullmatch(COMMIT_SHA_PATTERN, sha or ""):
        raise ValueError("需要完整提交 SHA，不能使用分支或标签名；请先同步目标版本。")
    return sha


def archive_version(checkout: str, sha: str) -> bytes:
    require_commit_sha(sha)
    # No checkout, fetch, ref mutation, or default-branch fallback.
    return git_read(checkout, "archive", "--format=tar", sha)


def normalize_provider_archive(payload):
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as source, tarfile.open(fileobj=output, mode="w:") as destination:
        for member in source.getmembers():
            if not member.isfile() or "/" not in member.name:
                continue
            member.name = member.name.split("/", 1)[1]
            destination.addfile(member, source.extractfile(member))
    return output.getvalue()


def code_files(archive: bytes) -> dict[str, str]:
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if not member.isfile() or path.name in SECRET_FILENAMES or any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix not in TEXT_EXTENSIONS and path.name not in TEXT_FILENAMES:
                continue
            stream = source.extractfile(member)
            files[member.name] = stream.read().decode("utf-8", errors="replace")
    return files


def code_version(materials, role, args):
    pr, ci = materials.get("pr"), materials.get("ci")
    version_key = args.get("version", "ci" if role == "ci_debug" else "head")
    if version_key not in {"issue_analysis": {"head"}, "pr_review": {"head", "base"}, "ci_debug": {"ci"}}[role]:
        raise ValueError(f"{role} 的代码工具不允许读取 {version_key} 版本。")
    versions = {"base": pr and pr["base_sha"], "head": pr and pr["head_sha"], "ci": ci and ci["commit_sha"]}
    if role == "issue_analysis":
        versions["head"] = args.get("_code_sha")
    version = versions.get(version_key)
    if not version:
        raise ValueError(f"快照缺少 {version_key} 提交版本。")
    if version in materials.get("code_errors", {}):
        raise ValueError(materials["code_errors"][version])
    return require_commit_sha(version)


def read_material(snapshot, role: str, name: str, args: dict, archives: dict) -> dict:
    materials = snapshot.materials
    pr, ci = materials.get("pr"), materials.get("ci")
    if name == "issue.read" and role == "issue_analysis":
        issue = next((item for item in materials["issues"] if item["id"] == args["_target_id"]), None)
        if issue is None:
            raise ValueError("当前任务没有绑定明确的 Issue。")
        text, version, location = json.dumps(issue, ensure_ascii=False, indent=2), issue["content_digest"], f"issue:{issue['id']}"
    elif name == "team.read" and role == "issue_analysis":
        text = json.dumps(materials["team"], ensure_ascii=False, indent=2)
        version, location = content_digest(materials["team"]), "project:team"
    elif name in {"materials.search", "materials.read"}:
        kind = args["kind"]
        if kind not in {"issues", "documents", "conversations"}:
            raise ValueError("材料工具范围无效。")
        items = materials[kind]
        if name == "materials.search":
            if kind == "issues" and role == "issue_analysis":
                items = [item for item in items if item["id"] != args["_target_id"]]
            hits = search_materials(items, args["query"])
            text = json.dumps({"retrieval_method": "snapshot_keyword", "candidates": hits, "note": "分数仅用于发现候选，判断前读取完整内容。"}, ensure_ascii=False, indent=2)
            location, version = f"{kind}:search:{args['query']}", str(snapshot.id)
        else:
            item = next((item for item in items if item["id"] == args["id"]), None)
            if item is None:
                raise ValueError("请求的材料不属于本次快照。")
            text, version, location = json.dumps(item, ensure_ascii=False, indent=2), item["content_digest"], f"{kind}:{item['id']}"
    elif name in {"pr.read_diff", "pr.read_context"} and role == "pr_review":
        if not pr:
            raise ValueError("目标 PR 材料缺失。")
        if name == "pr.read_diff" and pr.get("diff_error"):
            raise ValueError(pr["diff_error"])
        if name == "pr.read_diff":
            require_commit_sha(pr["base_sha"])
            require_commit_sha(pr["head_sha"])
        text = pr["diff"] if name == "pr.read_diff" else json.dumps({key: value for key, value in pr.items() if key not in {"diff", "files"}}, ensure_ascii=False, indent=2)
        version = f"{pr['base_sha']}...{pr['head_sha']}" if name == "pr.read_diff" else f"snapshot {snapshot.id} / PR head {pr['head_sha']}"
        location = "PR diff" if name == "pr.read_diff" else "PR metadata and review comments"
    elif name in {"ci.read_logs", "ci.read_jobs", "ci.read_failed_tests"} and role == "ci_debug":
        if not ci:
            raise ValueError(materials["ci_missing"])
        if not ci["commit_sha"] or not ci["run_attempt"]:
            raise ValueError("指定 CI 缺少提交或 attempt 绑定，请重新同步。")
        require_commit_sha(ci["commit_sha"])
        version = f"{ci['commit_sha']} / run {ci['github_run_id']} attempt {ci['run_attempt']}"
        location = name
        if name == "ci.read_jobs":
            text = json.dumps(ci["jobs"], ensure_ascii=False, indent=2)
        else:
            text = ci["logs_text"] or ""
            if name == "ci.read_failed_tests":
                lines = text.splitlines()
                matches = {index for index, line in enumerate(lines) if re.search(r"FAIL|ERROR|AssertionError|Error:", line)}
                selected = {near for index in matches for near in range(max(0, index - 2), min(len(lines), index + 4))}
                text = "\n".join(f"{index + 1}: {lines[index]}" for index in sorted(selected))
        if not text:
            raise ValueError(f"指定 CI attempt 缺少 {name} 材料。")
    elif name in {"code.list", "code.read", "code.search"}:
        version = code_version(materials, role, args)
        files = code_files(archives[version])
        if name == "code.list":
            prefix = args.get("path", "")
            text = "\n".join(sorted(path for path in files if path.startswith(prefix))) or "未找到可读文件。"
            location = f"files in {prefix or '/'}"
        elif name == "code.read":
            location = args["path"]
            if location not in files:
                raise ValueError(f"固定提交 {version} 中没有可读文件 {location}。")
            text = files[location]
        else:
            query = args["query"]
            if not query.strip():
                raise ValueError("代码搜索需要具体关键词。")
            prefix = args.get("path", "")
            hits = []
            for path, content in files.items():
                if not path.startswith(prefix):
                    continue
                for index, line in enumerate(content.splitlines(), 1):
                    if query.lower() in line.lower():
                        hits.append(f"{path}:{index}: {line}")
            text = "\n".join(hits) or "未找到匹配项。"
            location = f"search {query!r} in {prefix or '/'}"
    else:
        raise ValueError(f"角色 {role} 不允许调用工具 {name}。")
    if name in {"issue.read", "team.read", "materials.read"}:
        offset = max(0, int(args.get("offset", 0)))
        end = min(len(text), offset + min(16000, max(1, int(args.get("max_chars", 12000)))))
        return {"source": name, "version": version, "location": f"{location}:chars:{offset}-{end}/{len(text)}",
                "excerpt": text[offset:end], "read_range": [offset, end, len(text)],
                "next_offset": end if end < len(text) else None}
    start = max(1, int(args.get("start_line", 1)))
    count = min(200, max(1, int(args.get("line_count", 100))))
    lines = text.splitlines()
    excerpt = "\n".join(f"{index + start}: {line}" for index, line in enumerate(lines[start - 1:start - 1 + count]))
    return {"source": name, "version": version, "location": f"{location}:{start}", "excerpt": excerpt[:16000], "total_lines": len(lines)}
