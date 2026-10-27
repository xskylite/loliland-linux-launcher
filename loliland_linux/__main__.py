import argparse
import os
from pathlib import Path
import sys
import time
import subprocess

from . import __version__
from .core import Bootstrap, LauncherError


def main():
    parser = argparse.ArgumentParser(description="Unofficial LoliLand bootstrapper for Linux x86_64")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "prepare", "update", "doctor", "install", "uninstall"])
    parser.add_argument("--data-dir", type=Path, help="Bootstrapper storage; does not relocate the official game profile")
    parser.add_argument("--java-home", type=Path, help="Existing JBR/JCEF JDK 17+; otherwise use a detected runtime or download JBR 17")
    parser.add_argument("--gui", action="store_true", help="Show a native progress window (Python Tk required)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    previous = [0.0, ""]

    def progress(message, done, total):
        now = time.monotonic()
        if message != previous[1] or now - previous[0] > 0.5 or total is not None and done == total:
            suffix = f" {done / 1024**2:.1f}/{total / 1024**2:.1f} MiB" if total else ""
            print(message + suffix, flush=True)
            previous[:] = [now, message]

    try:
        bootstrap = Bootstrap(args.data_dir, progress, java_home=args.java_home)
        if args.gui:
            if args.command not in ("run", "update"):
                parser.error("--gui is supported with run and update")
            from .gui import run
            run(args.data_dir, update=args.command == "update", java_home=args.java_home)
        elif args.command == "install":
            from .integration import install
            binary, desktop = install(bootstrap)
            print(f"Installed command: {binary}\nDesktop entry: {desktop}\nRun {binary} to download and launch.")
        elif args.command == "uninstall":
            from .integration import uninstall_integration
            uninstall_integration()
            print("Removed the command and desktop entry. Downloaded files and game data were preserved.")
        elif args.command == "doctor":
            bootstrap.platform_check()
            print("Bootstrap data:", bootstrap.root)
            print("Launcher Java home:", bootstrap.runtime)
            print("DISPLAY:", "available" if os.environ.get("DISPLAY") else "missing (X11/XWayland required)")
            if not bootstrap.runtime_ready():
                raise LauncherError("Runtime is not installed; run prepare first")
            bootstrap.dependencies()
            launcher = bootstrap.installed_launcher()
            if not launcher:
                raise LauncherError("Launcher is missing or modified; run prepare to repair")
            agent = bootstrap.ensure_agent()
            subprocess.run([str(bootstrap.runtime / "bin/java"), "-version"], check=True)
            print("OK: launcher checksum, JBR/JCEF libraries and OSR agent", agent)
            print("This does not test account login, the game runtime or a server connection.")
        else:
            launcher, agent = bootstrap.prepare(update=args.command == "update")
            if args.command == "run":
                pid = bootstrap.launch(launcher, agent)
                print("Official launcher started; PID", pid)
            else:
                print("Ready:", launcher)
        return 0
    except KeyboardInterrupt:
        print("\nCancelled; installed files were preserved.", file=sys.stderr)
        return 130
    except (LauncherError, OSError, RuntimeError) as error:
        print("LoliLand Linux: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
