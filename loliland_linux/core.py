from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import __version__

RUNTIME_NAME = "jbr_jcef-17.0.14-linux-x64-b1367.22"
RUNTIME_URL = "https://cache-redirector.jetbrains.com/intellij-jbr/" + RUNTIME_NAME + ".tar.gz"
RUNTIME_SHA512 = (
    "1c875b43de4d20deb429a686331e4ea5c7c34c519569d1a3b785867d761ed0029"
    "41203f79b19a338e4a49fab48a6cd4d35408a7356f55781e95c3d60c6c5a8d1"
)
OFFICIAL_HOSTS = ("https://launcher.loliland.pro", "https://launcher.loliland.ru")
KNOWN_LAUNCHER_SHA256 = "a66f29f2718c2042c877aa2a6f2c6f68d2f24a58a699cbbdb3c4e84d8ab8d250"
MAIN_CLASS = "loliland.launcher.app.Main"


class LauncherError(Exception):
    pass


class Cancelled(LauncherError):
    pass


def digest(path: Path, algorithm="sha256"):
    result = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def atomic_json(path: Path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(path)


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise LauncherError("Refusing a redirect away from HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, destination: Path, *, expected_hash=None, algorithm="sha256",
             expected_size=None, limit=600 * 1024 * 1024, progress=lambda *_: None,
             cancelled=lambda: False):
    """Download into staging, preserving destination on every failure."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise LauncherError("Downloads must use HTTPS")
    part = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "LoliLand-Linux/" + __version__})
    checksum = hashlib.new(algorithm)
    opener = urllib.request.build_opener(HTTPSOnly())
    try:
        with opener.open(request, timeout=30) as response, part.open("wb") as output:
            length = response.headers.get("Content-Length")
            total = int(length) if length else expected_size
            if total is not None and total > limit:
                raise LauncherError("Download is larger than the allowed limit")
            done = 0
            started = time.monotonic()
            while True:
                if cancelled():
                    raise Cancelled("Cancelled")
                if time.monotonic() - started > 900:
                    raise LauncherError("Download exceeded 15 minutes; please retry")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise LauncherError("Download is larger than the allowed limit")
                checksum.update(chunk)
                output.write(chunk)
                progress("Downloading " + url.split("/")[2], done, total)
        if expected_size is not None and done != expected_size:
            raise LauncherError("Downloaded size doesn't match the official manifest")
        if total is not None and done != total:
            raise LauncherError("Incomplete download")
        if expected_hash is not None and checksum.hexdigest() != expected_hash:
            raise LauncherError("Checksum mismatch; the existing installation was preserved")
        part.replace(destination)
        return checksum.hexdigest()
    finally:
        part.unlink(missing_ok=True)


def extract_runtime(archive: Path, destination: Path):
    """Only materialize directories/files from one known runtime tree.

    Links are processed last and confined to that tree; special files, traversal,
    duplicate names and excessive sizes are rejected. Works on Python 3.10+.
    """
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 30000:
            raise LauncherError("Too many runtime archive entries")
        seen = set()
        total = 0
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != RUNTIME_NAME:
                raise LauncherError("Unsafe runtime archive path")
            normalized = str(name)
            if normalized in seen:
                raise LauncherError("Duplicate runtime archive path")
            seen.add(normalized)
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise LauncherError("Unsupported runtime archive entry")
            total += member.size
            if total > 2 * 1024**3:
                raise LauncherError("Runtime archive expands beyond the allowed limit")
        # No symlinks exist in staging while writing files.
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        root = (destination / RUNTIME_NAME).resolve()
        for member in members:
            if not (member.issym() or member.islnk()):
                continue
            target = destination / member.name
            link = PurePosixPath(member.linkname)
            if link.is_absolute():
                raise LauncherError("Absolute runtime archive link")
            linked = ((target.parent if member.issym() else destination) / str(link)).resolve()
            if not linked.is_relative_to(root):
                raise LauncherError("Runtime archive link escapes the runtime")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Refuse link parents too, including parents created by earlier links.
            if not target.parent.resolve().is_relative_to(root):
                raise LauncherError("Runtime archive link parent escapes the runtime")
            if member.issym():
                target.symlink_to(member.linkname)
            else:
                os.link(linked, target)


def validate_launcher(path: Path):
    try:
        with zipfile.ZipFile(path) as archive:
            if "loliland/launcher/app/Main.class" not in archive.namelist():
                raise LauncherError("The download is not the LoliLand launcher")
            if archive.testzip() is not None:
                raise LauncherError("Corrupt launcher JAR")
    except zipfile.BadZipFile as error:
        raise LauncherError("The server returned an invalid launcher JAR") from error


def compatible_environment(environment=None, profile_data_home=None):
    result = dict(os.environ if environment is None else environment)
    result.pop("WAYLAND_DISPLAY", None)
    result["XDG_SESSION_TYPE"] = "x11"
    result["_JAVA_AWT_WM_NONREPARENTING"] = "1"
    if profile_data_home is not None:
        result["XDG_DATA_HOME"] = str(profile_data_home)
    return result


class Bootstrap:
    def __init__(self, data_dir=None, progress=lambda *_: None, cancelled=lambda: False, java_home=None):
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        self.root = Path(data_dir).expanduser().resolve() if data_dir else base / "loliland-linux"
        self.managed_runtime = self.root / "runtimes" / RUNTIME_NAME
        selected = java_home or os.environ.get('LOLILAND_JAVA_HOME') or None
        config = self.root / 'runtime.json'
        if selected is None and config.exists():
            try:
                selected = json.loads(config.read_text())['java_home']
                if not isinstance(selected, str) or not Path(selected).is_absolute():
                    raise ValueError('Not an absolute Java home')
            except (ValueError, KeyError, TypeError) as error:
                raise LauncherError('Invalid runtime.json; preserving it for recovery') from error
        if selected is None:
            candidates = []
            if os.environ.get('JAVA_HOME'):
                candidates.append(Path(os.environ['JAVA_HOME']))
            java = shutil.which('java')
            if java:
                candidates.append(Path(java).resolve().parent.parent)
            candidates.extend(sorted((self.root / 'runtimes').glob('*')))
            for path in candidates:
                if path == self.managed_runtime or not (path / 'lib/libjcef.so').is_file():
                    continue
                self.runtime = path.resolve()
                try:
                    self.validate_selected_runtime()
                except (LauncherError, OSError, subprocess.SubprocessError):
                    continue
                selected = self.runtime
                break
        self.runtime = Path(selected).expanduser().resolve() if selected is not None else self.managed_runtime
        self.external_runtime = selected is not None and self.runtime != self.managed_runtime
        self.progress = progress
        self.cancelled = cancelled

    def ensure_profile_home(self):
        """Pin the official profile independently of shell/menu environment."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = self.root / "profile.json"
        if config.exists():
            try:
                value = json.loads(config.read_text())["xdg_data_home"]
                if not isinstance(value, str) or not Path(value).is_absolute():
                    raise ValueError("Not an absolute path")
                return Path(value)
            except (ValueError, KeyError, TypeError) as error:
                raise LauncherError("Invalid profile.json; preserving it for recovery") from error
        value = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))).expanduser().resolve()
        atomic_json(config, {"xdg_data_home": str(value)})
        return value

    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "bootstrap.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LauncherError("Another LoliLand Linux preparation is already running") from error
            yield

    def platform_check(self):
        if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
            raise LauncherError("This release supports Linux x86_64 with glibc")
        if not platform.libc_ver()[0] == "glibc":
            raise LauncherError("A glibc distribution is required (Alpine/musl is unsupported)")

    def runtime_ready(self):
        if self.external_runtime:
            try:
                self.validate_selected_runtime()
                return True
            except LauncherError:
                return False
        return all((self.runtime / name).is_file() for name in
                   ("bin/java", "bin/javac", "lib/libjcef.so", ".verified-sha512")) and (
                       self.runtime / ".verified-sha512").read_text().strip() == RUNTIME_SHA512

    def validate_selected_runtime(self):
        if not all((self.runtime / name).is_file() for name in ('bin/java', 'bin/javac', 'lib/libjcef.so')):
            raise LauncherError('Java home must provide bin/java, bin/javac and lib/libjcef.so. '
                                'Select a Linux JBR/JCEF JDK 17 or newer using --java-home.')
        release = self.runtime / 'release'
        text = release.read_text() if release.exists() else ''
        version = re.search(r'^JAVA_VERSION="(\d+)', text, re.MULTILINE)
        if version is None:
            result = subprocess.run([str(self.runtime / 'bin/java'), '-version'], capture_output=True, text=True, timeout=15)
            version = re.search(r'version "(\d+)', result.stderr + result.stdout)
        if version is None or int(version.group(1)) < 17:
            raise LauncherError('The launcher and compatibility agent require Java 17 or newer')
        modules = re.search(r'^MODULES="([^"]*)"', text, re.MULTILINE)
        if modules is not None and 'jcef' not in modules.group(1).split():
            raise LauncherError('Selected Java home has no JCEF module; ordinary OpenJDK is insufficient')

    def remember_runtime(self):
        if self.external_runtime:
            self.validate_selected_runtime()
            self.root.mkdir(parents=True, exist_ok=True)
            atomic_json(self.root / 'runtime.json', {'java_home': str(self.runtime)})

    def ensure_runtime(self):
        if self.external_runtime:
            self.remember_runtime()
            return
        if self.runtime_ready():
            return
        self.progress("Preparing JetBrains Runtime with JCEF", 0, None)
        self.runtime.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".runtime-", dir=self.runtime.parent) as temporary:
            staging = Path(temporary)
            archive = staging / "runtime.tar.gz"
            download(RUNTIME_URL, archive, expected_hash=RUNTIME_SHA512, algorithm="sha512",
                     progress=self.progress, cancelled=self.cancelled)
            if self.cancelled():
                raise Cancelled("Cancelled")
            self.progress("Verifying and extracting runtime", 0, None)
            extract_runtime(archive, staging)
            candidate = staging / RUNTIME_NAME
            (candidate / ".verified-sha512").write_text(RUNTIME_SHA512 + "\n")
            backup = self.runtime.with_name(self.runtime.name + ".previous")
            if backup.exists():
                raise LauncherError("A previous runtime recovery directory exists: " + str(backup))
            if self.runtime.exists():
                self.runtime.rename(backup)
            try:
                candidate.rename(self.runtime)
            except BaseException:
                if backup.exists():
                    backup.rename(self.runtime)
                raise
            if backup.exists():
                shutil.rmtree(backup)

    def installed_launcher(self):
        state_path = self.root / "launcher.json"
        if not state_path.exists():
            return None
        try:
            state = json.loads(state_path.read_text())
            checksum = state["sha256"]
            if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
                raise ValueError("Invalid launcher checksum")
            path = self.root / "launchers" / checksum / "launcher.jar"
            if not path.is_file() or digest(path) != checksum:
                return None
            return path
        except (ValueError, KeyError, TypeError):
            return None

    def ensure_launcher(self, update=False):
        current = self.installed_launcher()
        if current and not update:
            return current
        self.progress("Checking official LoliLand downloads", 0, None)
        errors = []
        for host in OFFICIAL_HOSTS:
            try:
                with tempfile.TemporaryDirectory(prefix=".launcher-", dir=self.root) as temporary:
                    staging = Path(temporary)
                    manifest_path = staging / "manifest.json"
                    download(host + "/api/updater/win64", manifest_path, limit=64 * 1024,
                             cancelled=self.cancelled)
                    manifest = json.loads(manifest_path.read_text())["launcher"]
                    size = manifest["size"]
                    if not isinstance(size, int) or not 1024 <= size <= 200 * 1024**2:
                        raise LauncherError("Invalid official launcher size")
                    candidate = staging / "launcher.jar"
                    checksum = download(host + "/api/updater/load/launcher", candidate,
                                        expected_size=size, limit=200 * 1024**2,
                                        progress=self.progress, cancelled=self.cancelled)
                    validate_launcher(candidate)
                    # New versions become active only after complete download and validation.
                    destination = self.root / "launchers" / checksum / "launcher.jar"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    candidate.replace(destination)
                    state = {"sha256": checksum, "size": size, "source": host,
                             "updater_hash": manifest.get("hash"), "saved_at": int(time.time())}
                    atomic_json(self.root / "launcher.json", state)
                    return destination
            except Cancelled:
                raise
            except (OSError, ValueError, KeyError, TypeError, LauncherError) as error:
                errors.append(host + ": " + str(error))
        raise LauncherError("Official download servers are unavailable.\n" + "\n".join(errors)
                            + "\nExisting files were preserved. Try again later.")

    def ensure_agent(self):
        source = Path(__file__).with_name("OsrAgent.java")
        source_hash = digest(source)
        destination = self.root / "compat" / source_hash / "osr-agent.jar"
        # Keep and verify the generated artifact; do not trust existence alone.
        marker = destination.with_suffix(".sha256")
        if destination.exists() and marker.exists() and digest(destination) == marker.read_text().strip():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.progress("Building Linux keyboard and rendering compatibility", 0, None)
        with tempfile.TemporaryDirectory(prefix=".agent-", dir=destination.parent) as temporary:
            staging = Path(temporary)
            result = subprocess.run([str(self.runtime / "bin/javac"), "--release", "17", "-d", str(staging),
                                     str(source)], capture_output=True, text=True)
            if result.returncode:
                raise LauncherError("Could not build OSR compatibility:\n" + result.stderr[-4000:])
            candidate = staging / "agent.jar"
            with zipfile.ZipFile(candidate, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("META-INF/MANIFEST.MF",
                                 "Manifest-Version: 1.0\nPremain-Class: io.github.lolilandlinux.OsrAgent\n\n")
                for path in sorted(staging.rglob("*.class")):
                    archive.write(path, path.relative_to(staging).as_posix())
            candidate.replace(destination)
            marker.write_text(digest(destination) + "\n")
        return destination

    def dependencies(self):
        if not shutil.which("ldd"):
            raise LauncherError("ldd is required to check native JCEF libraries")
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(self.runtime / "lib/server") + ":" + env.get("LD_LIBRARY_PATH", "")
        result = subprocess.run(["ldd", str(self.runtime / "lib/libjcef.so")],
                                env=env, capture_output=True, text=True)
        missing = [line.strip() for line in result.stdout.splitlines() if "not found" in line]
        if result.returncode or missing:
            raise LauncherError("Missing Linux desktop libraries:\n" + "\n".join(missing or [result.stderr])
                                + "\nSee README: distribution dependencies.")

    def prepare(self, update=False):
        self.platform_check()
        with self.lock():
            self.ensure_profile_home()
            self.ensure_runtime()
            self.dependencies()
            launcher = self.ensure_launcher(update)
            from .integration import install_icon
            install_icon(self, launcher)
            agent = self.ensure_agent()
            return launcher, agent

    def command(self, launcher, agent):
        return [str(self.runtime / "bin/java"), "-Xmx1024m", "--enable-native-access=ALL-UNNAMED,jcef",
                "-javaagent:" + str(agent), "-jar", str(launcher)]

    def launch(self, launcher, agent):
        if not os.environ.get("DISPLAY"):
            raise LauncherError("An X11 or Wayland desktop with XWayland (DISPLAY) is required")
        self.progress("Starting the official LoliLand launcher", 0, None)
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        output = logs / "bootstrap.log"
        if output.exists() and output.stat().st_size > 5 * 1024**2:
            output.replace(logs / "bootstrap.previous.log")
        profile_home = self.ensure_profile_home()
        state = profile_home / "LoliLand/Binaries/launcher-data"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (state / "linux-launcher.lock").open("a") as instance, output.open("a") as log:
            try:
                fcntl.flock(instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LauncherError("LoliLand is already open for this profile. Use the existing window.") from error
            # The Java child inherits the open file description and holds the
            # profile lock until it exits, even after the bootstrapper closes.
            process = subprocess.Popen(self.command(launcher, agent),
                                       env=compatible_environment(profile_data_home=profile_home),
                                       cwd=self.root, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True, pass_fds=(instance.fileno(),))
            self.process = process
            try:
                code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                return process.pid
            raise LauncherError("Launcher exited early (code " + str(code) + "). See " + str(output))
