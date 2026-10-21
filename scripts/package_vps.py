"""Create a source release from an explicit allowlist, without user data or secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sources():
    names = {
        ".dockerignore", ".env.example", ".gitignore", "Dockerfile", "compose.yaml", "compose.dev.yaml",
        "pyproject.toml", "requirements.lock.txt", "README.md",
        "docs/project-access.md", "docs/development.md",
        "deploy/vps/Caddyfile", "deploy/vps/README.md", "deploy/vps/requirements.txt", "deploy/vps/requirements.lock.txt",
        "deploy/vps/VALIDATION.md",
        "deploy/vps/frontend/package.json", "deploy/vps/frontend/package-lock.json",
        "deploy/vps/browser/package.json", "deploy/vps/browser/package-lock.json",
        "scripts/package_vps.py", "scripts/platform-login.mjs", "scripts/vps-browser-smoke.mjs",
        "scripts/dev.ps1", "start-dev.cmd", "stop-dev.cmd",
    }
    for directory in ("idi", "frontend/src", "frontend/public", "tests"):
        for path in (ROOT / directory).rglob("*"):
            relative = path.relative_to(ROOT)
            if (path.is_file() and not path.is_symlink() and "__pycache__" not in relative.parts
                    and path.suffix not in (".pyc", ".key", ".pem", ".bin", ".sqlite3", ".log")
                    and path.name != ".env"):
                names.add(relative.as_posix())
    for pattern in ("package*.json", "tsconfig*.json", "vite.config.*", "index.html"):
        names.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "frontend").glob(pattern))
    return sorted(names)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "vps-version-2026-10-20" / "idi-vps-source.zip")
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "kind": "source", "files": {}}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sources():
            content = (ROOT / name).read_bytes()
            manifest["files"][name] = hashlib.sha256(content).hexdigest()
            archive.writestr("idi-vps/" + name, content)
        archive.writestr("idi-vps/source-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(output), "sha256": digest, "files": len(manifest["files"]), "bytes": output.stat().st_size}))


if __name__ == "__main__":
    main()
