"""Shared scope rules for intake, runtime changes and integration."""
from pathlib import Path, PurePosixPath


def safe_path(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        return False
    path = PurePosixPath(name)
    return (not path.is_absolute() and str(path) == name and name != "."
            and ".." not in path.parts
            and not any(part.startswith(".env") or part in {
                ".git", ".beads", ".dolt", ".buchos", ".venv", "node_modules", "__pycache__"
            } for part in path.parts)
            and path.suffix.lower() not in {".sqlite3", ".sqlite", ".db", ".pem", ".key", ".pyc"})


def scopes_valid(scopes):
    return isinstance(scopes, list) and bool(scopes) and all(safe_path(p) for p in scopes)


def within_scope(name, scopes):
    return safe_path(name) and any(name == p or name.startswith(p + "/") for p in scopes)


def overlaps(left, right):
    return any(a == b or a.startswith(b + "/") or b.startswith(a + "/") for a in left for b in right)


def verify_paths(repo, names, scopes=None):
    for name in names:
        if not safe_path(name):
            raise RuntimeError("protected_path_changed")
        path = Path(repo) / name
        while path != Path(repo):
            if path.is_symlink():
                raise RuntimeError("protected_path_changed")
            path = path.parent
        if scopes and not within_scope(name, scopes):
            raise RuntimeError("task_scope_exceeded")
