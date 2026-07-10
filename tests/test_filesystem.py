"""filesystem/ surface tests (W5) — a real FsFacade rooted at tmp_path.

Env is pinned BEFORE importing ultimate_mcp so context.py / impl.py resolve
HA_CONFIG_ROOT and DATA_DIR (used for undo copies) into the sandbox.
"""

import asyncio
import inspect
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

_SANDBOX = tempfile.mkdtemp(prefix="umcp-fs-test-")
os.environ["UMCP_HA_CONFIG"] = str(Path(_SANDBOX) / "config")
os.environ["UMCP_DATA"] = str(Path(_SANDBOX) / "data")

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.context import FsFacade  # noqa: E402
from ultimate_mcp.tools.filesystem import impl  # noqa: E402
from ultimate_mcp.tools.filesystem.manifest import SURFACE  # noqa: E402

SECRETS_YAML = "wifi_password: hunter2\napi_token: abcd-1234\n"
CONFIG_YAML = (
    "homeassistant:\n  name: Home\n"
    "recorder:\n  db_url: !secret db_url\n"  # db_url is missing from secrets.yaml
    "http:\n  api_password: !secret wifi_password\n"
)
GOOD_YAML = "automation:\n  - alias: test\n    trigger: []\n"
BAD_YAML = "foo:\n  - bar\n  baz: qux\n"  # inconsistent indentation -> parse error
MANIFEST = {"domain": "mything", "name": "My Thing", "version": "1.2.3",
            "requirements": ["foolib==1.0"], "codeowners": ["@me"]}


def build_config(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "secrets.yaml").write_text(SECRETS_YAML, encoding="utf-8")
    (root / "configuration.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (root / "good.yaml").write_text(GOOD_YAML, encoding="utf-8")
    (root / "bad.yaml").write_text(BAD_YAML, encoding="utf-8")
    cc = root / "custom_components" / "mything"
    cc.mkdir(parents=True)
    (cc / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    (cc / "__init__.py").write_text("DOMAIN = 'mything'\n", encoding="utf-8")
    # A backup tar under backup/ containing a modified configuration.yaml.
    backup_dir = root / "backup"
    backup_dir.mkdir()
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as tf:
        data = b"homeassistant:\n  name: OldHome\n"
        info = tarfile.TarInfo(name="config/configuration.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    (backup_dir / "snap1.tar").write_bytes(tar_bytes.getvalue())


class StubCtx:
    def __init__(self, root: Path):
        self.fs = FsFacade(root=root)


@pytest.fixture()
def ctx(tmp_path: Path) -> StubCtx:
    root = tmp_path / "config"
    build_config(root)
    return StubCtx(root)


# --------------------------------------------------------------- contract
def test_manifest_impl_parity():
    for spec in SURFACE.tools:
        fn = getattr(impl, spec.name, None)
        assert fn is not None, f"missing impl for {spec.name}"
        assert inspect.iscoroutinefunction(fn), f"{spec.name} must be async"


# --------------------------------------------------------------- T0 reads
def test_fs_read_masks_secrets(ctx):
    out = asyncio.run(impl.fs_read(ctx, "secrets.yaml"))
    assert out["masked"] is True
    assert "hunter2" not in out["content"]
    assert "***MASKED***" in out["content"]
    # non-secret files are returned verbatim
    plain = asyncio.run(impl.fs_read(ctx, "good.yaml"))
    assert plain["masked"] is False
    assert "alias: test" in plain["content"]


def test_fs_tree_lists_files(ctx):
    out = asyncio.run(impl.fs_tree(ctx, max_depth=3))
    paths = {e["path"] for e in out["entries"]}
    assert "configuration.yaml" in paths
    assert "custom_components" in paths


def test_fs_grep_finds_term(ctx):
    out = asyncio.run(impl.fs_grep(ctx, "recorder"))
    files = {m["file"] for m in out["matches"]}
    assert "configuration.yaml" in files
    assert out["count"] >= 1


def test_yaml_lint_ok_and_error(ctx):
    good = asyncio.run(impl.yaml_lint(ctx, "good.yaml"))
    assert good["ok"] is True
    bad = asyncio.run(impl.yaml_lint(ctx, "bad.yaml"))
    assert bad["ok"] is False
    assert "line" in bad


def test_secrets_audit_finds_unused_and_missing(ctx):
    out = asyncio.run(impl.secrets_audit(ctx))
    # api_token is defined but never referenced
    assert "api_token" in out["unused_secrets"]
    # db_url is referenced (!secret db_url) but not defined
    missing_keys = {m["key"] for m in out["missing_secrets"]}
    assert "db_url" in missing_keys


def test_custom_component_inventory_reads_manifest(ctx):
    out = asyncio.run(impl.custom_component_inventory(ctx))
    assert out["count"] == 1
    comp = out["components"][0]
    assert comp["domain"] == "mything"
    assert comp["version"] == "1.2.3"
    assert comp["requirements"] == ["foolib==1.0"]


def test_backup_tar_list_reads_fixture(ctx):
    out = asyncio.run(impl.backup_tar_list(ctx, "backup/snap1.tar"))
    names = {e["name"] for e in out["entries"]}
    assert "config/configuration.yaml" in names


def test_backup_tar_diff_compares(ctx):
    out = asyncio.run(
        impl.backup_tar_diff(ctx, "backup/snap1.tar", "config/configuration.yaml",
                             current_path="configuration.yaml")
    )
    assert out["identical"] is False
    assert "OldHome" in out["diff"]
    assert "Home" in out["diff"]


# --------------------------------------------------------------- T1 writes
def test_fs_write_www_dry_run_and_execute(ctx):
    dry = asyncio.run(impl.fs_write_www(ctx, "app/index.html", "<h1>hi</h1>"))
    assert dry["dry_run"] is True
    assert dry["served_at"] == "/local/app/index.html"
    ex = asyncio.run(impl.fs_write_www(ctx, "app/index.html", "<h1>hi</h1>", dry_run=False))
    assert ex["written"] is True
    assert ctx.fs.read_text("www/app/index.html") == "<h1>hi</h1>"


def test_theme_write_rejects_bad_yaml(ctx):
    out = asyncio.run(impl.theme_write(ctx, "mytheme", "foo:\n  - a\n  b: c\n"))
    assert "error" in out


# --------------------------------------------------------------- T2 writes
def test_yaml_edit_any_dry_run_returns_diff(ctx):
    new = "homeassistant:\n  name: NewName\n"
    out = asyncio.run(impl.yaml_edit_any(ctx, "configuration.yaml", new))
    assert out["dry_run"] is True
    assert "NewName" in out["diff"]
    # on-disk file untouched by dry run
    assert "NewName" not in ctx.fs.read_text("configuration.yaml")


def test_yaml_edit_any_rejects_invalid_yaml(ctx):
    out = asyncio.run(impl.yaml_edit_any(ctx, "configuration.yaml", "foo:\n  - a\n  b: c\n"))
    assert "error" in out


def test_yaml_edit_any_execute_keeps_undo(ctx):
    new = "homeassistant:\n  name: NewName\n"
    out = asyncio.run(impl.yaml_edit_any(ctx, "configuration.yaml", new, dry_run=False))
    assert out["written"] is True
    assert out["undo_id"]
    assert "NewName" in ctx.fs.read_text("configuration.yaml")


def test_custom_component_scaffold_dry_run_lists_files(ctx):
    out = asyncio.run(impl.custom_component_scaffold(ctx, "newthing"))
    assert out["dry_run"] is True
    paths = {f["path"] for f in out["files"]}
    assert "custom_components/newthing/manifest.json" in paths
    assert "custom_components/newthing/__init__.py" in paths


def test_custom_component_scaffold_refuses_existing(ctx):
    out = asyncio.run(impl.custom_component_scaffold(ctx, "mything", dry_run=False))
    assert "error" in out and "already exists" in out["error"]
