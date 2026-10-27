import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import zipfile

from .core import LauncherError

MARKER = "# Managed by LoliLand Linux\n"


def install_icon(bootstrap, launcher):
    with zipfile.ZipFile(launcher) as archive:
        try:
            info = archive.getinfo('assets/LoliLand.png')
        except KeyError:
            return None
        if info.file_size > 2 * 1024**2:
            return None
        data = archive.read(info)
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return None
    icon = bootstrap.root / 'icon.png'
    icon.write_bytes(data)
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    desktop = data_home / 'applications/loliland-linux.desktop'
    if desktop.exists() and MARKER in desktop.read_text():
        lines = desktop.read_text().splitlines()
        desktop.write_text('\n'.join('Icon=' + desktop_string(icon) if line.startswith('Icon=') else line for line in lines) + '\n')
    return icon


def desktop_string(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def desktop_argument(value):
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + desktop_string(escaped).replace("%", "%%") + '"'


def install(bootstrap):
    """Copy only our own source; the downloaded upstream files stay separate."""
    bootstrap.ensure_profile_home()
    bootstrap.remember_runtime()
    launcher = bootstrap.installed_launcher()
    icon = install_icon(bootstrap, launcher) if launcher else None
    source = Path(__file__).resolve().parent
    program = bootstrap.root / "program"
    target = program / "loliland_linux"
    program.mkdir(parents=True, exist_ok=True)
    if source != target.resolve():
        with tempfile.TemporaryDirectory(prefix=".install-", dir=program) as temporary:
            staging = Path(temporary) / "loliland_linux"
            shutil.copytree(source, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            old = program / "loliland_linux.previous"
            if old.exists():
                raise LauncherError("A program recovery folder exists: " + str(old))
            if target.exists():
                target.rename(old)
            try:
                staging.rename(target)
            except BaseException:
                if old.exists():
                    old.rename(target)
                raise
            if old.exists():
                shutil.rmtree(old)

    binary = Path.home() / ".local/bin/loliland-linux"
    binary.parent.mkdir(parents=True, exist_ok=True)
    if binary.exists() and MARKER not in binary.read_text():
        raise LauncherError("Refusing to overwrite an unrelated command: " + str(binary))
    text = ("#!/bin/sh\n" + MARKER + "cd " + shlex.quote(str(program)) + " || exit 1\n"
            + "exec " + shlex.quote(sys.executable) + " -m loliland_linux --data-dir "
            + shlex.quote(str(bootstrap.root)) + ' "$@"\n')
    temporary = binary.with_suffix(".tmp")
    temporary.write_text(text)
    temporary.chmod(0o755)
    temporary.replace(binary)
    try:
        import tkinter  # noqa: F401 — also checks whether libtk is actually installed.
        has_gui = True
    except ImportError:
        has_gui = shutil.which("zenity") is not None
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    desktop = data_home / "applications/loliland-linux.desktop"
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(MARKER + "\n".join([
        "[Desktop Entry]", "Type=Application", "Name=LoliLand Linux",
        "Comment=Unofficial Linux bootstrapper for the official LoliLand launcher",
        "Exec=" + desktop_argument(binary) + (" --gui" if has_gui else ""),
        "Icon=" + (desktop_string(icon) if icon else "applications-games"), "Terminal=" + ("false" if has_gui else "true"),
        "Categories=Game;", "StartupWMClass=loliland-launcher-app-Main", "",
    ]))
    return binary, desktop


def uninstall_integration():
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    files = [Path.home() / ".local/bin/loliland-linux", data_home / "applications/loliland-linux.desktop"]
    for path in files:
        if path.is_file() and MARKER in path.read_text():
            path.unlink()
