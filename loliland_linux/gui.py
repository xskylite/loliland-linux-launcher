"""Optional native progress window. The actual launcher UI belongs to LoliLand."""
import queue
import shutil
import subprocess
import threading

from .core import Bootstrap, Cancelled


def run(data_dir=None, update=False, java_home=None):
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as error:
        if shutil.which("zenity"):
            return run_zenity(data_dir, update, java_home)
        raise RuntimeError("Install zenity or Python Tk, or run without --gui") from error
    root = tk.Tk()
    root.title("LoliLand Linux")
    root.geometry("540x220")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="LoliLand Linux", font=("sans-serif", 18, "bold")).pack(anchor="w")
    ttk.Label(frame, text="Preparing the official launcher · Community bootstrapper").pack(anchor="w", pady=(3, 18))
    status = tk.StringVar(value="Checking installation…")
    ttk.Label(frame, textvariable=status, wraplength=480).pack(anchor="w")
    bar = ttk.Progressbar(frame, length=480, mode="indeterminate")
    bar.pack(fill="x", pady=12)
    bar.start()
    events = queue.Queue()
    stop = threading.Event()
    closing = False

    def progress(message, done, total):
        events.put(("progress", message, done, total))

    def worker():
        try:
            bootstrap = Bootstrap(data_dir, progress, stop.is_set, java_home=java_home)
            launcher, agent = bootstrap.prepare(update)
            if stop.is_set():
                raise Cancelled("Cancelled")
            bootstrap.launch(launcher, agent)
            events.put(("done",))
        except Cancelled:
            events.put(("cancelled",))
        except Exception as error:
            events.put(("error", str(error)))

    def cancel():
        nonlocal closing
        closing = True
        stop.set()
        status.set("Cancelling… (the current network request may take up to 30 seconds)")
        button.configure(state="disabled")

    button = ttk.Button(frame, text="Cancel", command=cancel)
    button.pack(anchor="e")
    root.protocol("WM_DELETE_WINDOW", cancel)

    def poll():
        try:
            while True:
                event = events.get_nowait()
                if event[0] in ("done", "cancelled"):
                    root.destroy()
                    return
                if event[0] == "error":
                    if not closing:
                        messagebox.showerror("LoliLand Linux", event[1], parent=root)
                    root.destroy()
                    return
                if not closing:
                    _, message, done, total = event
                    if total:
                        bar.stop()
                        bar.configure(mode="determinate", maximum=total, value=done)
                        status.set(f"{message}\n{done / 1024**2:.1f} / {total / 1024**2:.1f} MiB")
                    else:
                        bar.configure(mode="indeterminate")
                        bar.start()
                        status.set(message)
        except queue.Empty:
            root.after(100, poll)

    threading.Thread(target=worker, daemon=True).start()
    root.after(100, poll)
    root.mainloop()


def run_zenity(data_dir=None, update=False, java_home=None):
    """Use the desktop's own progress dialog when Python Tk is unavailable."""
    process = subprocess.Popen(["zenity", "--progress", "--title=LoliLand Linux",
                                "--text=Preparing the official launcher…", "--width=540",
                                "--percentage=0", "--auto-close"], stdin=subprocess.PIPE, text=True)
    def cancelled():
        return process.poll() is not None
    def progress(message, done, total):
        if cancelled():
            raise Cancelled("Cancelled")
        suffix = f" — {done / 1024**2:.1f}/{total / 1024**2:.1f} MiB" if total else ""
        percentage = min(99, int(done * 100 / total)) if total else 0
        try:
            process.stdin.write(f"# {message}{suffix}\n{percentage}\n")
            process.stdin.flush()
        except BrokenPipeError as error:
            raise Cancelled("Cancelled") from error
    try:
        bootstrap = Bootstrap(data_dir, progress, cancelled, java_home=java_home)
        launcher, agent = bootstrap.prepare(update)
        if cancelled():
            raise Cancelled("Cancelled")
        bootstrap.launch(launcher, agent)
        process.stdin.write("100\n")
        process.stdin.flush()
        process.wait(timeout=5)
    except Cancelled:
        pass
    except Exception as error:
        process.terminate()
        process.wait(timeout=5)
        subprocess.run(["zenity", "--error", "--title=LoliLand Linux", "--width=540",
                        "--text=" + str(error)[-4000:]], check=False)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
