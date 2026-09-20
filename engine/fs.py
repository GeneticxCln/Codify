from __future__ import annotations

import difflib
from pathlib import Path


class PathEscapeError(Exception):
    def __init__(self, path: str):
        super().__init__(f"path escapes workspace: {path}")
        self.path = path


class FileSystemService:
    def __init__(self, root_path: str):
        self.root = Path(root_path).resolve()

    def resolve(self, rel: str) -> Path:
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise PathEscapeError(rel)
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise PathEscapeError(rel)
        return target

    def read_text(self, rel: str) -> str:
        path = self.resolve(rel)
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def apply(self, files: list[dict], *, dry_run: bool) -> list[dict]:
        summaries: list[dict] = []
        for item in files:
            rel = item["path"]
            action = item["action"]
            content = item.get("content")
            target = self.resolve(rel)
            before = target.read_text(encoding="utf-8") if target.is_file() else ""
            after = "" if action == "delete" else (content or "")
            diff = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            if not dry_run:
                if action == "delete":
                    if target.is_file():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(after, encoding="utf-8")
            summaries.append({"path": rel, "action": action, "unified_diff": diff})
        return summaries
