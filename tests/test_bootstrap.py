import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

from loliland_linux import core, integration


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_checksum_failure_preserves_previous_file(self):
        target = self.root / "launcher.jar"
        target.write_bytes(b"working installation")
        opener = mock.Mock()
        opener.open.return_value = Response(b"corrupt replacement")
        with mock.patch.object(core.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(core.LauncherError):
                core.download("https://example.org/file", target, expected_hash="0" * 64)
        self.assertEqual(target.read_bytes(), b"working installation")
        self.assertFalse(target.with_suffix(".jar.part").exists())

    def test_cancelled_download_does_not_replace_file(self):
        target = self.root / "file"
        target.write_bytes(b"previous")
        opener = mock.Mock()
        opener.open.return_value = Response(b"new")
        with mock.patch.object(core.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(core.Cancelled):
                core.download("https://example.org/file", target, cancelled=lambda: True)
        self.assertEqual(target.read_bytes(), b"previous")

    def test_partial_download_and_https_downgrade_are_rejected(self):
        opener = mock.Mock()
        response = Response(b"short")
        response.headers["Content-Length"] = "100"
        opener.open.return_value = response
        with mock.patch.object(core.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(core.LauncherError):
                core.download("https://example.org/file", self.root / "file")
        with self.assertRaises(core.LauncherError):
            core.HTTPSOnly().redirect_request(None, None, 302, "", {}, "http://example.org/file")

    def make_tar(self, members):
        archive = self.root / "runtime.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name, kind, data in members:
                entry = tarfile.TarInfo(name)
                if kind == "link":
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = data
                    tar.addfile(entry)
                elif kind == "device":
                    entry.type = tarfile.CHRTYPE
                    tar.addfile(entry)
                else:
                    entry.mode = 0o755
                    entry.size = len(data)
                    tar.addfile(entry, io.BytesIO(data))
        return archive

    def test_archive_traversal_absolute_paths_and_special_files(self):
        for name, kind, data in [("../escape", "file", b"x"), ("/tmp/escape", "file", b"x"),
                                 (core.RUNTIME_NAME + "/device", "device", b"")]:
            with self.subTest(name=name):
                archive = self.make_tar([(name, kind, data)])
                with self.assertRaises(core.LauncherError):
                    core.extract_runtime(archive, self.root / "staging")
        self.assertFalse((self.root / "escape").exists())

    def test_archive_rejects_link_escape_and_allows_internal_links(self):
        archive = self.make_tar([(core.RUNTIME_NAME + "/bin/java", "file", b"java"),
                                 (core.RUNTIME_NAME + "/escape", "link", "../../outside")])
        with self.assertRaises(core.LauncherError):
            core.extract_runtime(archive, self.root / "bad")
        archive = self.make_tar([(core.RUNTIME_NAME + "/bin/java", "file", b"java"),
                                 (core.RUNTIME_NAME + "/bin/link", "link", "java")])
        core.extract_runtime(archive, self.root / "good")
        self.assertEqual((self.root / "good" / core.RUNTIME_NAME / "bin/link").read_bytes(), b"java")

    def test_failed_update_preserves_current_version_and_state(self):
        bootstrap = core.Bootstrap(self.root)
        path = self.root / "launchers" / ("a" * 64) / "launcher.jar"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"current")
        state = b'{"previous":"untouched"}'
        (self.root / "launcher.json").write_bytes(state)
        with mock.patch.object(bootstrap, "installed_launcher", return_value=path):
            with mock.patch.object(core, "download", side_effect=OSError("server offline")):
                with self.assertRaises(core.LauncherError):
                    bootstrap.ensure_launcher(update=True)
        self.assertEqual(path.read_bytes(), b"current")
        self.assertEqual((self.root / "launcher.json").read_bytes(), state)

    def test_new_version_activates_after_validation_and_detects_corruption(self):
        jar = self.root / "fixture.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("loliland/launcher/app/Main.class", b"fixture")
        payload = jar.read_bytes()
        bootstrap = core.Bootstrap(self.root)
        def download(url, destination, **kwargs):
            if url.endswith("win64"):
                destination.write_text(json.dumps({"launcher": {"size": len(payload), "hash": "official-id"}}))
            else:
                destination.write_bytes(payload)
            return hashlib.sha256(destination.read_bytes()).hexdigest()
        # The real launcher is larger; pad the mock ZIP while keeping it valid.
        with zipfile.ZipFile(jar, "a") as archive:
            archive.writestr("padding", b"x" * 2048)
        payload = jar.read_bytes()
        with mock.patch.object(core, "download", side_effect=download):
            installed = bootstrap.ensure_launcher()
        self.assertEqual(installed, bootstrap.installed_launcher())
        installed.write_bytes(b"corrupt")
        self.assertIsNone(bootstrap.installed_launcher())

    def test_keyboard_environment_only_changes_child_copy(self):
        env = {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":1", "XDG_SESSION_TYPE": "wayland"}
        child = core.compatible_environment(env)
        self.assertNotIn("WAYLAND_DISPLAY", child)
        self.assertEqual(child["XDG_SESSION_TYPE"], "x11")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")

    def test_profile_directory_stays_fixed_between_shell_and_menu(self):
        bootstrap = core.Bootstrap(self.root / 'bootstrap')
        first = self.root / 'first-data-home'
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(first)}):
            self.assertEqual(bootstrap.ensure_profile_home(), first)
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'other-data-home')}):
            self.assertEqual(bootstrap.ensure_profile_home(), first)

    def fake_jbr(self, name='jbr17', version='17.0.14', cef=True):
        home = self.root / name
        for relative in ('bin/java', 'bin/javac', *(('lib/libjcef.so',) if cef else ())):
            path = home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        (home / 'release').write_text(f'JAVA_VERSION="{version}"\nMODULES="java.base jcef"\n')
        return home

    def test_existing_jcef_17_is_reused_without_download_and_pinned_for_menu(self):
        home = self.fake_jbr()
        root = self.root / 'bootstrap'
        with mock.patch.dict(os.environ, {'JAVA_HOME': str(home), 'LOLILAND_JAVA_HOME': ''}):
            bootstrap = core.Bootstrap(root)
            self.assertEqual(bootstrap.runtime, home)
            with mock.patch.object(core, 'download') as download:
                bootstrap.ensure_runtime()
                download.assert_not_called()
        with mock.patch.dict(os.environ, {'JAVA_HOME': '', 'LOLILAND_JAVA_HOME': ''}):
            bootstrap = core.Bootstrap(root)
            self.assertEqual(bootstrap.runtime, home)
            self.assertTrue(bootstrap.runtime_ready())
        self.assertEqual((root / 'runtime.json').stat().st_mode & 0o777, 0o600)

    def test_ordinary_java_does_not_replace_jcef_and_fallback_is_17(self):
        home = self.fake_jbr(cef=False)
        with mock.patch.dict(os.environ, {'JAVA_HOME': str(home), 'LOLILAND_JAVA_HOME': ''}):
            bootstrap = core.Bootstrap(self.root / 'bootstrap')
        self.assertEqual(bootstrap.runtime, bootstrap.managed_runtime)
        self.assertTrue(core.RUNTIME_NAME.startswith('jbr_jcef-17.'))

    def test_explicit_runtime_rejects_pre_17_and_missing_jcef(self):
        for name, version, cef in [('jbr11', '11.0.20', True), ('plain17', '17.0.14', False)]:
            home = self.fake_jbr(name, version, cef)
            bootstrap = core.Bootstrap(self.root / 'bootstrap', java_home=home)
            with self.assertRaises(core.LauncherError), mock.patch.object(core, 'download') as download:
                bootstrap.ensure_runtime()
            download.assert_not_called()
    def test_concurrent_preparation_is_rejected(self):
        first = core.Bootstrap(self.root)
        second = core.Bootstrap(self.root)
        with first.lock():
            with self.assertRaises(core.LauncherError):
                with second.lock():
                    self.fail("The second process must not modify an active installation")

    def test_desktop_installation_handles_spaces_and_uninstall_keeps_data(self):
        home = self.root / "user space"
        data = home / "data % space"
        bootstrap = core.Bootstrap(data / "bootstrap")
        with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}):
            binary, desktop = integration.install(bootstrap)
            self.assertTrue(os.access(binary, os.X_OK))
            result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "0.1.0")
            if shutil.which("desktop-file-validate"):
                subprocess.run(["desktop-file-validate", str(desktop)], check=True)
            integration.uninstall_integration()
            self.assertFalse(binary.exists())
            self.assertFalse(desktop.exists())
            self.assertTrue(bootstrap.root.exists())


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"), "JDK needed for agent test")
class AgentTests(unittest.TestCase):
    def test_real_jvm_changes_only_launcher_rendering_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bootstrap = core.Bootstrap(root / "data")
            bootstrap.runtime = Path(shutil.which("javac")).resolve().parent.parent
            agent = bootstrap.ensure_agent()
            source = root / "src"
            fixtures = {
                "org/cef/browser/CefRendering.java": "package org.cef.browser; public enum CefRendering { DEFAULT, OFFSCREEN }",
                "other/Probe.java": "package other; public class Probe { public static String value() { return org.cef.browser.CefRendering.DEFAULT.name(); } }",
                "loliland/launcher/app/data/ClientLaunchConfig.java": '''package loliland.launcher.app.data;
                    public class ClientLaunchConfig {
                      @Deprecated public final java.util.List<String> getGameArguments() {
                        try {
                          if (Boolean.getBoolean("fixture.empty")) return java.util.List.of();
                          return java.util.List.of("--username \\\"${auth_player_name}\\\"", "--gameDir \\\"${gameDir}\\\"", "--version 1.7.10");
                        } catch (IllegalArgumentException error) { return java.util.List.of(); }
                      }
                    }''',
                "loliland/launcher/test/Probe.java": '''package loliland.launcher.test;
                    public class Probe {
                      public static void main(String[] args) throws Exception {
                        if (args.length > 0 && args[0].equals("hold")) { Thread.sleep(30000); return; }
                        long twoSlotPoolEntry = 123456789012345L;
                        System.out.println(org.cef.browser.CefRendering.DEFAULT + "," + other.Probe.value() + ",DEFAULT," + twoSlotPoolEntry);
                        System.out.println(String.join("|", new loliland.launcher.app.data.ClientLaunchConfig().getGameArguments()));
                        System.out.println(loliland.launcher.app.data.ClientLaunchConfig.class.getDeclaredMethod("getGameArguments").isAnnotationPresent(Deprecated.class));
                      }
                    }''',
            }
            for name, content in fixtures.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            classes = root / "classes"
            subprocess.run([shutil.which("javac"), "--release", "17", "-d", str(classes),
                            *[str(source / name) for name in fixtures]], check=True)
            homes = [bootstrap.runtime, *[Path(home) for home in os.environ.get('LOLILAND_TEST_JAVA_HOMES', '').split(os.pathsep) if home]]
            with zipfile.ZipFile(agent) as archive:
                data = archive.read('io/github/lolilandlinux/OsrAgent.class')
                self.assertEqual(int.from_bytes(data[6:8], 'big'), 61)  # Java 17 bytecode
            for home in dict.fromkeys(homes):
                with self.subTest(java_home=home):
                    command = [str(home / 'bin/java'), '--limit-modules=java.base,java.instrument',
                               "-javaagent:" + str(agent), "-cp", str(classes), "loliland.launcher.test.Probe"]
                    result = subprocess.run(command, capture_output=True, text=True, check=True)
                    self.assertEqual(result.stdout.splitlines()[0], "OFFSCREEN,DEFAULT,DEFAULT,123456789012345")
                    self.assertEqual(result.stdout.splitlines()[1], '--username ${auth_player_name}|--gameDir "${gameDir}"|--version 1.7.10')
                    self.assertEqual(result.stdout.splitlines()[2], 'true')
                    self.assertIn("JCEF OSR: loliland/launcher/test/Probe", result.stderr)
                    self.assertNotIn("JCEF OSR: other/Probe", result.stderr)
                    empty = subprocess.run([*command[:1], '-Dfixture.empty=true', *command[1:]],
                                           capture_output=True, text=True, check=True)
                    self.assertEqual(empty.stdout.splitlines()[1], '')
            # Verify that the actual Java child retains the inherited profile lock.
            core.atomic_json(bootstrap.root / 'profile.json', {'xdg_data_home': str(root / 'profile')})
            hold = [str(bootstrap.runtime / 'bin/java'), '-cp', str(classes), 'loliland.launcher.test.Probe', 'hold']
            with mock.patch.object(bootstrap, 'command', return_value=hold), mock.patch.dict(os.environ, {'DISPLAY': ':test'}):
                pid = bootstrap.launch(None, None)
                try:
                    with self.assertRaisesRegex(core.LauncherError, 'already open'):
                        bootstrap.launch(None, None)
                finally:
                    bootstrap.process.terminate()
                    bootstrap.process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
