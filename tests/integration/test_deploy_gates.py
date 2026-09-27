"""`servicelist deploy` refuses a release whose new deploy/gates/*.sql finds work of the old kind on the live
database: the running version stays, and the owner reads what to finish first."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import TEST_DB_URL

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "servicelist"
# `docker compose exec -T db psql|pg_isready …` and `docker compose up -d db`, answered by the test database
DOCKER = """#!/usr/bin/env bash
if [[ "$1 $2" == "compose up" ]]; then exit 0; fi
if [[ "$1 $2 $3 $4 $5" == "compose exec -T db pg_isready" ]]; then exit 0; fi
if [[ "$1 $2 $3 $4 $5" == "compose exec -T db psql" ]]; then
    shift 5
    args=()
    while (($#)); do
        case "$1" in -U | -d) shift 2 ;; *) args+=("$1"); shift ;; esac
    done
    exec psql "$SL_TEST_DB" "${args[@]}"
fi
echo "unexpected: docker $*" >&2
exit 1
"""

pytestmark = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("psql") and shutil.which("bash")),
    reason="git/psql/bash missing",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "running version")
    return repo


def _release(repo: Path, gates: dict[str, str]) -> str:
    """A commit on top of the running one that adds these gate files; returns its sha (HEAD stays)."""
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", f"next{len(gates)}")
    for name, sql in gates.items():
        path = repo / "deploy" / "gates" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sql)
    (repo / "README").write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "next release")
    target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", head)
    return target


def _run_gates(repo: Path, tmp_path: Path, target: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(DOCKER)
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SL_TEST_DB": TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://"),
    }
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; cd "{repo}"; run_gates "{target}"; echo passed'],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


async def test_a_gate_that_finds_nothing_lets_the_deploy_go_on(db, repo, tmp_path):
    target = _release(repo, {"0010-check.sql": "SELECT 'blocker' WHERE false;\n"})
    result = _run_gates(repo, tmp_path, target)
    assert result.returncode == 0 and result.stdout.strip().endswith("passed"), result.stderr


async def test_a_gate_that_finds_old_work_stops_the_deploy(db, repo, tmp_path):
    gate = (
        "-- hint: /admin → 🛡 Гарант → ⏸ Остановить приём сделок, довести сделки, обновить снова\n"
        "SELECT 'сделка #' || n || ': funded' FROM generate_series(1, 2) AS n;\n"
    )
    target = _release(repo, {"0010-escrow.sql": gate})
    result = _run_gates(repo, tmp_path, target)
    assert result.returncode != 0 and "passed" not in result.stdout
    assert "• сделка #1: funded" in result.stderr and "• сделка #2: funded" in result.stderr
    assert "Остановить приём сделок" in result.stderr and "Обновление остановлено" in result.stderr


async def test_a_gate_already_passed_is_not_run_again_and_no_gate_no_check(db, repo, tmp_path):
    target = _release(repo, {"0010-escrow.sql": "SELECT 'blocker';\n"})
    _git(repo, "checkout", "-q", target)  # this release is the running one now
    (repo / "README").write_text("v3\n")
    _git(repo, "commit", "-qam", "a later release")
    later = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", target)
    result = _run_gates(repo, tmp_path, later)
    assert result.returncode == 0 and result.stdout.strip().endswith("passed"), result.stderr
