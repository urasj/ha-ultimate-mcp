"""filesystem/ surface implementation — lazy-imported on first call (W5).

Reads and writes route through ctx.fs (rooted + path-escape guarded). Every
write is atomic (tmp file + os.fsync + os.replace) and drops an undo copy under
DATA_DIR/undo/<undo_id>/ so a botched edit is recoverable. Backup tars are only
ever read (Python tarfile, never written into). Any failure degrades to
{"error": ...} rather than raising at the tool boundary.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import secrets as _secrets
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from ultimate_mcp.context import DATA_DIR, Context

UNDO_ROOT = DATA_DIR / "undo"

_MASK = "***MASKED***"
_SKIP_DIRS = {"deps", "__pycache__", ".git", ".cloud", "tts", ".storage"}
_YAML_SUFFIXES = (".yaml", ".yml")


# --------------------------------------------------------------- write helpers
def _atomic_write(target: Path, content: str) -> None:
    """tmp file + fsync + os.replace — mirrors StorageEditor._atomic_write."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _undo_copy(rel: str, live: Path) -> str | None:
    """Copy the current file (if it exists) into a fresh undo dir; return undo_id."""
    if not live.exists():
        return None
    undo_id = f"{int(time.time())}-{_secrets.token_hex(4)}"
    undo_dir = UNDO_ROOT / undo_id
    undo_dir.mkdir(parents=True, exist_ok=True)
    (undo_dir / rel.replace("/", "__")).write_bytes(live.read_bytes())
    return undo_id


def _guarded_write(
    ctx: Context, rel: str, content: str, dry_run: bool, *, hint: str
) -> dict[str, Any]:
    """Shared preview/execute path for every filesystem write tool."""
    try:
        live = ctx.fs.resolve(rel)
    except PermissionError as exc:
        return {"error": str(exc)}
    before = ""
    if live.exists():
        try:
            before = live.read_text(encoding="utf-8", errors="replace")
        except OSError:
            before = ""
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )
    if dry_run:
        return {
            "dry_run": True,
            "path": rel,
            "exists": live.exists(),
            "bytes": len(content.encode("utf-8")),
            "diff": diff or "(no changes)",
            "note": f"re-run with dry_run=false to write ({hint})",
        }
    try:
        undo_id = _undo_copy(rel, live)
        _atomic_write(live, content)
    except OSError as exc:
        return {"error": f"write failed: {exc}", "path": rel}
    return {
        "dry_run": False,
        "written": True,
        "path": rel,
        "bytes": len(content.encode("utf-8")),
        "undo_id": undo_id,
        "diff": diff or "(new file)",
    }


# ------------------------------------------------------------------ T0 reads
def _mask_secrets_text(text: str) -> str:
    """Mask the value half of every `key: value` line in secrets.yaml."""
    out: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^(\s*[\w.-]+\s*:\s*)(\S.*)$", line)
        if m:
            out.append(m.group(1) + _MASK)
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


async def fs_read(ctx: Context, path: str, **_: Any) -> Any:
    try:
        text = ctx.fs.read_text(path)
    except (OSError, ValueError, PermissionError) as exc:
        return {"error": str(exc), "path": path}
    masked = Path(path).name == "secrets.yaml"
    if masked:
        text = _mask_secrets_text(text)
    return {"path": path, "masked": masked, "content": text}


async def fs_tree(ctx: Context, path: str = "", max_depth: int = 2, **_: Any) -> Any:
    try:
        base = ctx.fs.resolve(path) if path else ctx.fs.root
    except PermissionError as exc:
        return {"error": str(exc)}
    if not base.exists():
        return {"error": f"path not found: {path or '.'}"}
    root = base
    entries: list[dict[str, Any]] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError:
            return
        for c in children:
            if c.name in _SKIP_DIRS:
                continue
            rel = c.relative_to(root).as_posix()
            if c.is_dir():
                entries.append({"path": rel, "type": "dir", "depth": depth})
                walk(c, depth + 1)
            else:
                try:
                    size = c.stat().st_size
                except OSError:
                    size = None
                entries.append({"path": rel, "type": "file", "bytes": size, "depth": depth})

    walk(base, 1)
    return {"base": path or ".", "max_depth": max_depth, "count": len(entries), "entries": entries}


def _iter_yaml_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if p.suffix not in _YAML_SUFFIXES or not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel_parts[:-1]):
            continue
        out.append(p)
    return out


async def fs_grep(
    ctx: Context, pattern: str, regex: bool = False, max_matches: int = 200, **_: Any
) -> Any:
    try:
        rx = re.compile(pattern if regex else re.escape(pattern))
    except re.error as exc:
        return {"error": f"bad regex: {exc}"}
    root = ctx.fs.root
    matches: list[dict[str, Any]] = []
    for p in _iter_yaml_files(root):
        rel = p.relative_to(root).as_posix()
        try:
            text = ctx.fs.read_text(rel)
        except (OSError, ValueError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                matches.append({"file": rel, "line": i, "text": line.strip()[:200]})
                if len(matches) >= max_matches:
                    return {"pattern": pattern, "regex": regex, "truncated": True, "matches": matches}
    return {"pattern": pattern, "regex": regex, "count": len(matches), "matches": matches}


async def yaml_lint(ctx: Context, path: str, **_: Any) -> Any:
    try:
        text = ctx.fs.read_text(path)
    except (OSError, ValueError, PermissionError) as exc:
        return {"error": str(exc), "path": path}
    try:
        # !secret / !include are HA-specific tags; register a permissive loader so
        # a normal config file does not fail the lint on unknown tags.
        loader = _ha_safe_loader()
        docs = list(yaml.load_all(text, Loader=loader))
        return {"path": path, "ok": True, "documents": len(docs)}
    except yaml.YAMLError as exc:
        err: dict[str, Any] = {"path": path, "ok": False, "error": str(exc)}
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            err["line"] = mark.line + 1
            err["column"] = mark.column + 1
        return err


def _ha_safe_loader() -> type:
    """A SafeLoader that tolerates HA custom tags (!secret, !include, !env_var...)."""

    class _Loader(yaml.SafeLoader):
        pass

    def _passthrough(loader: Any, node: Any) -> Any:  # noqa: ANN401
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    for tag in ("!secret", "!include", "!include_dir_list", "!include_dir_named",
                "!include_dir_merge_list", "!include_dir_merge_named", "!env_var", "!input"):
        _Loader.add_constructor(tag, _passthrough)
    return _Loader


async def secrets_audit(ctx: Context, **_: Any) -> Any:
    # Load secrets.yaml keys.
    try:
        secrets_text = ctx.fs.read_text("secrets.yaml")
    except (OSError, ValueError, PermissionError):
        return {"error": "secrets.yaml not found or unreadable"}
    defined: set[str] = set()
    try:
        parsed = yaml.load(secrets_text, Loader=_ha_safe_loader())
        if isinstance(parsed, dict):
            defined = {str(k) for k in parsed.keys()}
    except yaml.YAMLError:
        # Fall back to a line scan if secrets.yaml itself is malformed.
        for line in secrets_text.splitlines():
            m = re.match(r"^([\w.-]+)\s*:", line)
            if m:
                defined.add(m.group(1))

    # Find every `!secret <key>` reference across config YAML.
    ref_re = re.compile(r"!secret\s+([\w.-]+)")
    used: dict[str, list[str]] = {}
    root = ctx.fs.root
    for p in _iter_yaml_files(root):
        rel = p.relative_to(root).as_posix()
        if rel == "secrets.yaml":
            continue
        try:
            text = ctx.fs.read_text(rel)
        except (OSError, ValueError):
            continue
        for m in ref_re.finditer(text):
            used.setdefault(m.group(1), []).append(rel)

    referenced = set(used.keys())
    unused = sorted(defined - referenced)
    missing = sorted(referenced - defined)
    return {
        "defined": len(defined),
        "referenced": len(referenced),
        "unused_secrets": unused,
        "missing_secrets": [{"key": k, "files": sorted(set(used[k]))} for k in missing],
    }


async def custom_component_inventory(ctx: Context, **_: Any) -> Any:
    try:
        cc_dir = ctx.fs.resolve("custom_components")
    except PermissionError as exc:
        return {"error": str(exc)}
    if not cc_dir.is_dir():
        return {"count": 0, "components": [], "note": "no custom_components directory"}
    components: list[dict[str, Any]] = []
    for d in sorted(cc_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("__"):
            continue
        entry: dict[str, Any] = {"domain": d.name}
        manifest = d / "manifest.json"
        if manifest.is_file():
            try:
                m = json.loads(manifest.read_text(encoding="utf-8"))
                entry.update(
                    {
                        "name": m.get("name"),
                        "version": m.get("version"),
                        "documentation": m.get("documentation"),
                        "requirements": m.get("requirements", []),
                        "codeowners": m.get("codeowners", []),
                        "iot_class": m.get("iot_class"),
                    }
                )
            except (ValueError, OSError) as exc:
                entry["manifest_error"] = str(exc)
        else:
            entry["manifest_error"] = "manifest.json missing"
        components.append(entry)
    return {"count": len(components), "components": components}


# --------------------------------------------------------------- backup tars
def _resolve_tar(ctx: Context, tar_path: str) -> tuple[Path | None, dict | None]:
    try:
        p = ctx.fs.resolve(tar_path)
    except PermissionError as exc:
        return None, {"error": str(exc)}
    if not p.is_file():
        return None, {"error": f"tar not found: {tar_path}"}
    return p, None


async def backup_tar_list(ctx: Context, tar_path: str, max_entries: int = 500, **_: Any) -> Any:
    p, err = _resolve_tar(ctx, tar_path)
    if err is not None:
        return err
    try:
        with tarfile.open(p, "r:*") as tf:  # read-only, autodetect compression
            members = tf.getmembers()
    except (tarfile.TarError, OSError) as exc:
        return {"error": f"cannot read tar: {exc}", "tar_path": tar_path}
    entries = [
        {"name": m.name, "size": m.size, "type": "dir" if m.isdir() else "file"}
        for m in members[:max_entries]
    ]
    return {
        "tar_path": tar_path,
        "total_members": len(members),
        "truncated": len(members) > max_entries,
        "entries": entries,
    }


async def backup_tar_diff(
    ctx: Context, tar_path: str, member: str, current_path: str | None = None, **_: Any
) -> Any:
    p, err = _resolve_tar(ctx, tar_path)
    if err is not None:
        return err
    try:
        with tarfile.open(p, "r:*") as tf:
            try:
                fh = tf.extractfile(member)
            except KeyError:
                return {"error": f"member not found in tar: {member}", "tar_path": tar_path}
            if fh is None:
                return {"error": f"member is not a regular file: {member}"}
            backup_text = fh.read().decode("utf-8", errors="replace")
    except (tarfile.TarError, OSError) as exc:
        return {"error": f"cannot read tar: {exc}", "tar_path": tar_path}

    rel = current_path or Path(member).name
    try:
        current_text = ctx.fs.read_text(rel)
        current_exists = True
    except (OSError, ValueError, PermissionError):
        current_text = ""
        current_exists = False

    diff = "".join(
        difflib.unified_diff(
            backup_text.splitlines(keepends=True),
            current_text.splitlines(keepends=True),
            fromfile=f"backup/{member}",
            tofile=f"current/{rel}",
        )
    )
    return {
        "tar_path": tar_path,
        "member": member,
        "current_path": rel,
        "current_exists": current_exists,
        "identical": diff == "",
        "diff": diff or "(identical)",
    }


# ------------------------------------------------------------- T1 writes
async def fs_write_www(ctx: Context, path: str, content: str, dry_run: bool = True, **_: Any) -> Any:
    rel = f"www/{path.lstrip('/')}"
    result = _guarded_write(ctx, rel, content, dry_run, hint="served at /local/" + path.lstrip("/"))
    if "error" not in result:
        result["served_at"] = "/local/" + path.lstrip("/")
    return result


async def theme_write(ctx: Context, name: str, content: str, dry_run: bool = True, **_: Any) -> Any:
    safe = name[:-5] if name.endswith(".yaml") else name
    rel = f"themes/{safe}.yaml"
    # Sanity-check the theme YAML before offering to write it.
    try:
        yaml.load(content, Loader=_ha_safe_loader())
    except yaml.YAMLError as exc:
        return {"error": f"theme content is not valid YAML: {exc}", "path": rel}
    return _guarded_write(ctx, rel, content, dry_run, hint="theme")


# --------------------------------------------------------------- T2 writes
async def yaml_edit_any(ctx: Context, path: str, content: str, dry_run: bool = True, **_: Any) -> Any:
    if Path(path).suffix not in _YAML_SUFFIXES:
        return {"error": f"yaml_edit_any only edits .yaml/.yml files (got {path})"}
    # Refuse to write content that does not parse as YAML — never leave the
    # config in a state that stops core from booting.
    try:
        yaml.load(content, Loader=_ha_safe_loader())
    except yaml.YAMLError as exc:
        err: dict[str, Any] = {"error": f"content is not valid YAML: {exc}", "path": path}
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            err["line"] = mark.line + 1
            err["column"] = mark.column + 1
        return err
    return _guarded_write(ctx, path, content, dry_run, hint="run `ha core check` after applying")


async def custom_component_scaffold(
    ctx: Context,
    domain: str,
    name: str | None = None,
    version: str = "0.1.0",
    dry_run: bool = True,
    **_: Any,
) -> Any:
    if not re.match(r"^[a-z][a-z0-9_]*$", domain):
        return {"error": f"invalid domain (must be a snake_case python identifier): {domain!r}"}
    friendly = name or domain.replace("_", " ").title()
    manifest = {
        "domain": domain,
        "name": friendly,
        "version": version,
        "documentation": f"https://example.com/{domain}",
        "dependencies": [],
        "codeowners": [],
        "requirements": [],
        "iot_class": "local_polling",
    }
    init_py = (
        '"""The ' + friendly + ' integration (scaffolded by ha-ultimate-mcp)."""\n'
        "from __future__ import annotations\n\n"
        "DOMAIN = \"" + domain + "\"\n\n\n"
        "async def async_setup(hass, config):\n"
        "    \"\"\"Set up the " + friendly + " integration.\"\"\"\n"
        "    return True\n"
    )
    files = {
        f"custom_components/{domain}/manifest.json": json.dumps(manifest, indent=2) + "\n",
        f"custom_components/{domain}/__init__.py": init_py,
    }

    # Guard against clobbering an existing component.
    try:
        existing = ctx.fs.resolve(f"custom_components/{domain}")
    except PermissionError as exc:
        return {"error": str(exc)}
    if existing.exists():
        return {"error": f"custom_components/{domain} already exists; refusing to overwrite"}

    if dry_run:
        return {
            "dry_run": True,
            "domain": domain,
            "files": [{"path": rel, "bytes": len(body.encode("utf-8"))} for rel, body in files.items()],
            "note": "re-run with dry_run=false to create these files",
        }
    written: list[str] = []
    try:
        for rel, body in files.items():
            _atomic_write(ctx.fs.resolve(rel), body)
            written.append(rel)
    except OSError as exc:
        return {"error": f"scaffold failed after {written}: {exc}"}
    return {"dry_run": False, "domain": domain, "created": written}
