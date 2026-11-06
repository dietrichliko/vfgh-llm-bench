"""Commit protocol and results files to the study repository and push an annotated tag.

The repository is ``BENCH_STUDY_REPO_URL`` (branch ``BENCH_STUDY_REPO_BRANCH``); for SSH, the
deploy key is the secret ``STUDY_REPO_DEPLOY_KEY`` (write access to that repository only).
Each push works in a fresh clone in a temporary directory; the key is written there with
mode 0600 and removed with it. Files go below ``studies/<slug>/``; ``results/`` is
replaced as a whole on each release. An existing tag is never moved.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from bench.settings import Settings

DEPLOY_KEY = "STUDY_REPO_DEPLOY_KEY"
AUTHOR = ("-c", "user.name=VfGH-LLM-Studie", "-c", "user.email=noreply@vfgh-llm-studie.invalid")


class RepoError(RuntimeError):
    pass


def configured(settings: Settings) -> bool:
    return bool(settings.study_repo_url)


def _git(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RepoError(f"git {args[0]}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def push(
    settings: Settings,
    slug: str,
    files: dict[str, str | bytes],
    tag: str,
    message: str,
    replace: str | None = None,
) -> str:
    """Commit ``files`` (paths below ``studies/<slug>/``), tag, push; the commit sha."""
    if not configured(settings):
        raise RepoError("Kein Studien-Repository konfiguriert")
    with tempfile.TemporaryDirectory(prefix="bench-study-") as tmp:
        work = Path(tmp)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        key = settings.secret(DEPLOY_KEY)
        if key:
            key_file = work / "deploy_key"
            key_file.write_text(key.strip() + "\n")
            key_file.chmod(0o600)
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key_file} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
            )
        repo = work / "repo"
        _git(["clone", "--quiet", settings.study_repo_url, str(repo)], work, env)
        branch = settings.study_repo_branch
        if _git(["ls-remote", "--tags", "origin", f"refs/tags/{tag}"], repo, env):
            raise RepoError(f"Tag {tag} gibt es schon im Repository")
        _git(["checkout", "--quiet", "-B", branch], repo, env)
        root = repo / "studies" / slug
        if replace:
            target = root / replace
            if target.exists():
                _git(["rm", "-r", "--quiet", str(target.relative_to(repo))], repo, env)
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                path.write_text(content)
            else:
                path.write_bytes(content)
        _git(["add", "-A", "studies"], repo, env)
        _git([*AUTHOR, "commit", "--quiet", "--allow-empty", "-m", message], repo, env)
        _git([*AUTHOR, "tag", "-a", tag, "-m", message], repo, env)
        _git(["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], repo, env)
        _git(["push", "--quiet", "origin", f"refs/tags/{tag}"], repo, env)
        return _git(["rev-parse", "HEAD"], repo, env)
