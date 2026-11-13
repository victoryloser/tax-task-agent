"""Capture the real UI using temporary fictional records, with no model calls.

Requires Playwright and its Chromium browser (or Microsoft Edge).
Output assets contain no records from material/ or the local business database.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"tests"))
sys.path.insert(0, str(ROOT/".build/browser-tools"))
from test_onboarding import workbook_bytes
from playwright.sync_api import sync_playwright


def main():
    output = ROOT/"docs/images"
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="readme-") as folder:
        env = os.environ.copy()
        env.update(TAX_AGENT_DB=str(Path(folder)/"business.sqlite3"), TAX_AGENT_DATABASE_URL="",
                   LLM_API_KEY="", LLM_BASE_URL="", LLM_MODEL="", PYTHONUTF8="1")
        with (Path(folder)/"streamlit.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "tax_agent/ui/app.py",
                "--server.headless", "true", "--server.port", str(port)], cwd=ROOT, env=env,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                for _ in range(60):
                    assert process.poll() is None, "Streamlit exited"
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=1):
                            break
                    except urllib.error.URLError:
                        time.sleep(.5)
                else:
                    raise RuntimeError("Streamlit did not become healthy")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel="msedge" if os.name == "nt" else None, headless=True)
                    page = browser.new_page(viewport={"width":1440, "height":1000}, device_scale_factor=1)
                    page.goto(f"http://127.0.0.1:{port}")
                    page.get_by_role("button", name="导入资料", exact=True).wait_for()
                    page.screenshot(path=str(output/"first-run.png"), full_page=True)
                    page.locator('input[type="file"]').set_input_files({
                        "name":"fictional.xlsx", "mimeType":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "buffer":workbook_bytes()})
                    page.get_by_role("button", name="导入资料", exact=True).click()
                    page.get_by_role("heading", name="把资料查清，把待办办完。").wait_for(timeout=30000)
                    page.get_by_text("12.35 元", exact=True).wait_for()
                    page.screenshot(path=str(output/"workspace.png"), full_page=True)
                    page.get_by_role("tab", name="当前资料", exact=True).click()
                    page.get_by_text("本页 1 条，共 1 条。", exact=True).wait_for()
                    page.screenshot(path=str(output/"invoices.png"), full_page=True)
                    browser.close()
            finally:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    process.terminate()
                process.wait(timeout=15)
    print("Captured first launch, XLSX upload and invoice view using temporary fictional records; no model calls")


if __name__ == "__main__":
    main()
