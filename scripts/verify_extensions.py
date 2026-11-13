"""验证可选部署扩展并绑定应用源码；PostgreSQL测试URL仅从环境读取。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from tax_agent.config import ROOT, implementation_hash
from tax_agent.storage.db import now


def main():
    source=implementation_hash()
    subprocess.run([sys.executable,"-m","pytest","integration","-q","--tb=short","--junitxml=runs/postgres-pytest.xml"],cwd=ROOT,check=True)
    suites=ET.parse(ROOT/"runs/postgres-pytest.xml").getroot()
    counts={key:sum(int(s.get(key,0)) for s in suites.iter("testsuite")) for key in ("tests","failures","errors","skipped")}
    assert counts["tests"]>0 and counts["failures"]==counts["errors"]==counts["skipped"]==0
    postgres={"created_at":now(),"implementation_sha256":source,"status":"passed","pytest":counts,
        "topology":"Local TCP PostgreSQL; separate worker processes and local checkpoint directories; no physical multi-host test",
        "credentials_logged":False}
    subprocess.run([sys.executable,"-m","tax_agent.runtime.service","--once"],cwd=ROOT,check=True)
    service={"created_at":now(),"implementation_sha256":source,"status":"passed","service_once_exit_code":0,
             "machine_boot_tested":False,"linux_systemd_tested":False,"login_cycle_tested":False}
    if os.name=="nt":
        result=subprocess.run(["powershell","-NoProfile","-Command","[Environment]::GetFolderPath('Startup')"],capture_output=True,text=True,check=True)
        path=Path(result.stdout.strip())/"TaxAgent-Class2-Worker.vbs"
        service["user_login_launcher_installed"]=path.exists()
        if path.exists():
            assert str(ROOT) in path.read_text(encoding="utf-16")
    assert implementation_hash()==source
    for name,report in (("postgres-validation",postgres),("service-validation",service)):
        (ROOT/"runs"/(name+".json")).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(name+": passed",flush=True)


if __name__=="__main__":
    main()
