"""Build a source archive from project files."""
import argparse
import hashlib
import json
from pathlib import Path
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ["tax_agent","tests","integration","scripts","docs","deploy",".streamlit",".github"]
DATA_SUFFIXES = {".csv", ".xlsx", ".xls", ".docx", ".parquet", ".duckdb", ".duckdb.wal", ".sqlite", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db", ".log"}


def distributable(path):
    return (path.is_file() and path.name not in {".env", "secrets.toml"}
            and not (path.name.startswith(".env.") and path.name != ".env.example")
            and not any(part in {"__pycache__", ".venv", "data", "runs", "material", ".build"}
                        or part.endswith(".egg-info") for part in path.relative_to(ROOT).parts)
            and path.suffix.lower() not in DATA_SUFFIXES | {".pyc"})


def main():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    paths = [ROOT / name for name in
             ("README.md", "README.zh-CN.md", "LICENSE", "pyproject.toml", "requirements.lock.txt", ".env.example", ".gitignore", ".gitattributes")]
    for folder in DIRECTORIES:
        paths.extend(path for path in (ROOT / folder).rglob("*")
                     if distributable(path))
    secrets = [value.encode() for config in (ROOT / ".env", ROOT / "deploy/.env")
               for key, value in dotenv_values(config).items()
               if value and any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]
    files = {}
    for path in sorted(set(paths)):
        payload = path.read_bytes()
        if any(secret in payload for secret in secrets):
            raise ValueError(f"Credential in release file: {path.relative_to(ROOT)}")
        files[path.relative_to(ROOT).as_posix()] = {
            "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
    manifest = {"project": project["name"], "version": project["version"], "files": files}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / (project["name"] + ".zip"))
    target = parser.parse_args().output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name in files:
            archive.write(ROOT / name, name)
        archive.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    # A rebuilt archive needs its own validation result.
    target.with_suffix(".validation.json").unlink(missing_ok=True)
    print(json.dumps({"archive": str(target), "files": len(files), "sha256": digest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
