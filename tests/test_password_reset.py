"""Regression tests for the "password reset did nothing" bug.

The systemd unit sets SMARTHOME_CONFIG=/var/lib/smarthome/config.json, but a
maintenance command run by hand (`sudo -u smarthome ... set-password`) does not
inherit that environment.  It used to fall back to ~/.smarthome/config.json,
write a new hash there, and report success -- while the service kept reading its
own file and the old password kept working.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from smarthome import config as config_module
from smarthome import security
from smarthome.config import Config, discover_config_path


@pytest.fixture()
def system_install(tmp_path: Path, monkeypatch):
    """A fake /var/lib/smarthome install plus a separate service-user home."""
    data_dir = tmp_path / "var-lib-smarthome"
    data_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.delenv("SMARTHOME_CONFIG", raising=False)
    monkeypatch.delenv("SMARTHOME_DATA_DIR", raising=False)
    monkeypatch.setattr(config_module, "SYSTEM_DATA_DIR", data_dir)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    return data_dir, home


def test_discovery_prefers_the_system_install(system_install):
    data_dir, home = system_install
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    # The per-user file also exists: the system one must still win, because that
    # is what the service reads.
    (home / ".smarthome").mkdir()
    (home / ".smarthome" / "config.json").write_text("{}", encoding="utf-8")
    assert discover_config_path() == data_dir / "config.json"


def test_discovery_falls_back_to_home_without_a_system_install(system_install):
    data_dir, home = system_install
    assert discover_config_path() == home / ".smarthome" / "config.json"


def test_explicit_config_always_wins(system_install, tmp_path: Path, monkeypatch):
    data_dir, _ = system_install
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    explicit = tmp_path / "elsewhere.json"
    monkeypatch.setenv("SMARTHOME_CONFIG", str(explicit))
    assert discover_config_path() == explicit


def test_data_dir_env_wins_over_the_system_install(system_install, tmp_path: Path, monkeypatch):
    data_dir, _ = system_install
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    custom = tmp_path / "custom"
    custom.mkdir()
    monkeypatch.setenv("SMARTHOME_DATA_DIR", str(custom))
    assert discover_config_path() == custom / "config.json"


def test_set_password_reaches_the_file_the_service_reads(system_install):
    """The bug: the CLI wrote to the wrong file and the old password survived."""
    data_dir, _ = system_install
    service_config = data_dir / "config.json"

    # What the service does at first boot (SMARTHOME_CONFIG is set for it).
    os.environ["SMARTHOME_CONFIG"] = str(service_config)
    try:
        service = Config.load(bootstrap_password=True)
        generated = service.generated_password
        assert generated, "a password should have been generated"
    finally:
        del os.environ["SMARTHOME_CONFIG"]

    # What the operator runs by hand, without that environment.
    cli = Config.load(bootstrap_password=False)
    assert cli.path == service_config, "the CLI resolved to the wrong config file"
    cli.set_admin_password("a-brand-new-password")

    stored = json.loads(service_config.read_text(encoding="utf-8"))
    assert security.verify_password("a-brand-new-password", stored["auth"]["password_hash"])
    assert not security.verify_password(generated, stored["auth"]["password_hash"])


def test_service_keeps_a_working_session_secret_after_cli_edit(system_install):
    """Editing the password must not invalidate the signing secret."""
    data_dir, _ = system_install
    service_config = data_dir / "config.json"
    os.environ["SMARTHOME_CONFIG"] = str(service_config)
    try:
        service = Config.load(bootstrap_password=True)
        secret = service.session_secret()
    finally:
        del os.environ["SMARTHOME_CONFIG"]

    cli = Config.load(bootstrap_password=False)
    cli.set_admin_password("another-password")

    reloaded = Config.load(str(service_config), bootstrap_password=False)
    assert reloaded.session_secret() == secret
