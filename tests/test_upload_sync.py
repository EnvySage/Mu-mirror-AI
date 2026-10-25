from pathlib import Path

from scripts import sync_upload


def make_repo(root: Path) -> None:
    for directory in sync_upload.RUNTIME_GROUPS:
        (root / directory).mkdir(parents=True)
    for relative in sync_upload.RUNTIME_ROOT_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source:{relative}", encoding="utf-8")
    for directory in sync_upload.RUNTIME_GROUPS:
        extension = ".txt" if directory == "prompts" else ".py"
        (root / directory / f"runtime{extension}").write_text(
            f"source:{directory}", encoding="utf-8"
        )


def test_sync_copies_only_runtime_files_and_preserves_local_config(tmp_path):
    make_repo(tmp_path)
    (tmp_path / "config.yml").write_text("local service config", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_only.py").write_text("test", encoding="utf-8")
    upload = tmp_path / "upload"
    upload.mkdir()
    (upload / "config.yml").write_text("deployment override", encoding="utf-8")

    changed = sync_upload.sync_upload(tmp_path)

    assert changed == sorted(changed, key=lambda path: path.relative_to(upload).as_posix())
    assert all(path.is_file() for path in changed)
    assert not (upload / "config.yml").read_text(encoding="utf-8") == "local service config"
    assert (upload / "config.yml").read_text(encoding="utf-8") == "deployment override"
    assert (upload / "config.example.yml").read_text(encoding="utf-8") == "source:config.example.yml"
    assert (upload / "services" / "runtime.py").read_text(encoding="utf-8") == "source:services"
    assert (upload / "agent_data" / "runtime.py").read_text(encoding="utf-8") == "source:agent_data"
    assert not (upload / "tests").exists()


def test_check_reports_drift_without_copying_or_touching_config(tmp_path):
    make_repo(tmp_path)
    upload = tmp_path / "upload"
    upload.mkdir()
    (upload / "config.yml").write_text("deployment override", encoding="utf-8")
    (upload / "server.py").write_text("outdated", encoding="utf-8")

    drift = sync_upload.sync_upload(tmp_path, check=True)

    assert (upload / "server.py").read_text(encoding="utf-8") == "outdated"
    assert (upload / "config.yml").read_text(encoding="utf-8") == "deployment override"
    assert upload / "server.py" in drift
    assert upload / "config.yml" not in drift
