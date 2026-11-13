"""Install and verify the exact source archive in a temporary virtual environment."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
CHECKS = [["source isolation",["-c","from pathlib import Path; from tax_agent.config import ROOT; assert ROOT == Path.cwd(); assert not (ROOT/'.env').exists()"]],["initialize data",["-m","tax_agent.cli","init"]],["tests",["-m","pytest","-q","-p","no:cacheprovider","--junitxml=runs/pytest.xml"]],["streamlit health",["-c","import socket, subprocess, sys, time, urllib.request, urllib.error\nwith socket.socket() as sock:\n    sock.bind(('127.0.0.1', 0))\n    port = sock.getsockname()[1]\nprocess = subprocess.Popen([sys.executable, '-m', 'streamlit', 'run', 'tax_agent/ui/app.py', '--server.headless', 'true', '--server.port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\ntry:\n    for _ in range(60):\n        assert process.poll() is None, 'Streamlit exited'\n        try:\n            with urllib.request.urlopen(f'http://127.0.0.1:{port}/_stcore/health', timeout=1) as response:\n                assert response.read() == b'ok'\n                break\n        except urllib.error.URLError:\n            time.sleep(.5)\n    else:\n        raise RuntimeError('Streamlit health check timed out')\nfinally:\n    process.terminate()\n    process.wait(timeout=15)\nprint('Streamlit health check passed')\n"]]]


def main():
    name = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "dist" / (name + ".zip"))
    args = parser.parse_args()
    archive_path = args.archive.resolve()
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert archive_path.with_suffix(".sha256").read_text(encoding="utf-8").split()[0] == digest
    report_path = archive_path.with_suffix(".validation.json")
    report = {"project": name, "archive_sha256": digest, "status": "running",
              "created_at": datetime.now(timezone.utc).isoformat(), "model_calls": False, "steps": []}
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LLM_", "JUDGE_", "SECOND_JUDGE_", "TAX_AGENT_"))
           and k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    build = ROOT / ".build"
    build.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="release-check-", dir=build) as directory:
            work = Path(directory)
            with ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("MANIFEST.json"))
                assert set(archive.namelist()) == set(manifest["files"]) | {"MANIFEST.json"}
                for filename, item in manifest["files"].items():
                    payload = archive.read(filename)
                    assert hashlib.sha256(payload).hexdigest() == item["sha256"], filename
                    assert len(payload) == item["bytes"], filename
                    assert Path(filename).name != ".env"
                archive.extractall(work)
            python = work / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

            def run(label, command):
                print(label, flush=True)
                result = subprocess.run([str(x) for x in command], cwd=work, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8")
                report["steps"].append({"name": label, "returncode": result.returncode, "output": result.stdout})
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                if result.returncode:
                    raise RuntimeError(f"{label} failed; see {report_path}")

            run("create environment", [sys.executable, "-m", "venv", work / ".venv"])
            run("locked dependencies", [python, "-m", "pip", "install", "-r", "requirements.lock.txt"])
            run("install source", [python, "-m", "pip", "install", "--no-deps", "-e", "."])
            run("dependency consistency", [python, "-m", "pip", "check"])
            for label, command in CHECKS:
                run(label, [python, *command])
            suites = ET.parse(work / "runs/pytest.xml").getroot()
            counts = {key: sum(int(suite.get(key, 0)) for suite in suites.iter("testsuite"))
                      for key in ("tests", "failures", "errors", "skipped")}
            assert counts["tests"] > counts["skipped"] and counts["failures"] == counts["errors"] == 0
            report["pytest"] = counts
            assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == digest
            report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", failure=f"{type(error).__name__}: {error}")
        raise
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("project", "status", "pytest")}), flush=True)


if __name__ == "__main__":
    main()
