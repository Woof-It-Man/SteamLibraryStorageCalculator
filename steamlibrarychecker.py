import tkinter as tk
from tkinter import ttk, messagebox
import threading
import subprocess
import time
import json
import os
import requests
import csv
import datetime

try:
    import vdf
except ImportError:
    vdf = None

# If steamcmd is not on your system PATH, set the full path to the executable here,
# e.g. "/home/you/steamcmd/steamcmd.sh" or r"C:\steamcmd\steamcmd.exe"
# Just download it frm valve's wiki and unpack it then path it to it.
# Leave as "steamcmd" to use whatever is on PATH.
STEAMCMD_PATH = "steamcmd"

STEAMCMD_CACHE_FILE = "steam_size_cache.json"
ERROR_LOG_FILE = "steam_size_errors.log"
AUTOSAVE_CSV = "steam_library_sizes_autosave.csv"
AUTOSAVE_EVERY_N = 15
STEAMCMD_TIMEOUT = 90
MAX_RETRIES = 2


def log_error(appid, name, message):
    with open(ERROR_LOG_FILE, "a") as f:
        f.write(f"[{datetime.datetime.now().isoformat()}] appid={appid} name={name!r}: {message}\n")

def resolve_steamid(api_key, vanity_or_id):
    if vanity_or_id.isdigit() and len(vanity_or_id) == 17:
        return vanity_or_id
    url = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/"
    resp = requests.get(url, params={"key": api_key, "vanityurl": vanity_or_id}, timeout=15)
    resp.raise_for_status()
    data = resp.json()["response"]
    if data.get("success") != 1:
        raise ValueError(f"Could not resolve vanity URL '{vanity_or_id}'")
    return data["steamid"]


def get_owned_games(api_key, steamid):
    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
    params = {
        "key": api_key,
        "steamid": steamid,
        "include_appinfo": 1,
        "include_played_free_games": 1,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("response", {})
    games = data.get("games", [])
    if not games:
        raise ValueError(
            "No games returned. Profile may be private, or 'Game details' "
            "privacy setting may be restricted."
        )
    return games


def load_cache():
    if os.path.exists(STEAMCMD_CACHE_FILE):
        with open(STEAMCMD_CACHE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(STEAMCMD_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def determine_target_os(depots):
    """
    Just checking windows stuff, if none exist linux instead, since I run everything in proton anyway and linux just returns tons of unknowns
    """
    seen_os = set()
    for depot_id, depot_block in depots.items():
        if not isinstance(depot_block, dict):
            continue
        config = depot_block.get("config", {})
        oslist = config.get("oslist", "")
        if oslist:
            for o in oslist.split(","):
                seen_os.add(o.strip())

    if "windows" in seen_os:
        return "windows"
    if "linux" in seen_os:
        return "linux"
    if "macos" in seen_os:
        return "macos"
    return None 


def depot_matches_target(depot_block, target_os):
    config = depot_block.get("config", {})
    oslist = config.get("oslist", "")
    if not oslist:
        return True  
    if target_os is None:
        return True  
    allowed = [o.strip() for o in oslist.split(",")]
    return target_os in allowed


def parse_vdf_output(output):
    lines = output.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('"') and stripped.endswith('"') and stripped.strip('"').isdigit():
            start_idx = i
            break
    if start_idx is None:
        return None

    vdf_text_lines = lines[start_idx:]
    end_idx = len(vdf_text_lines)
    for i, line in enumerate(vdf_text_lines):
        if "Unloading Steam API" in line:
            end_idx = i
            break
    vdf_text = "\n".join(vdf_text_lines[:end_idx])

    try:
        return vdf.loads(vdf_text)
    except Exception:
        return None


def get_size_from_steamcmd(appid, name):
    if vdf is None:
        return "NO_VDF_LIB"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                [STEAMCMD_PATH, "+login", "anonymous", "+app_info_print", str(appid), "+quit"],
                capture_output=True,
                text=True,
                timeout=STEAMCMD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            last_error = f"attempt {attempt}: steamcmd timed out after {STEAMCMD_TIMEOUT}s"
            log_error(appid, name, last_error)
            continue
        except FileNotFoundError:
            return "NO_STEAMCMD"

        output = result.stdout
        parsed = parse_vdf_output(output)
        if not parsed:
            last_error = f"attempt {attempt}: could not parse steamcmd output (len={len(output)} chars)"
            log_error(appid, name, last_error)
            time.sleep(1)
            continue

        try:
            app_data = next(iter(parsed.values()))
            depots = app_data.get("depots", {})
        except Exception as e:
            last_error = f"attempt {attempt}: unexpected VDF structure: {e}"
            log_error(appid, name, last_error)
            continue

        if not depots:
            last_error = f"attempt {attempt}: no 'depots' section found in app info"
            log_error(appid, name, last_error)
            return None  # legitimately no depot info (e.g. delisted/unavailable app)

        target_os = determine_target_os(depots)

        total = 0
        found_any = False

        for depot_id, depot_block in depots.items():
            if not isinstance(depot_block, dict):
                continue
            if "depotfromapp" in depot_block:
                continue
            if not depot_matches_target(depot_block, target_os):
                continue

            manifests = depot_block.get("manifests", {})
            if not isinstance(manifests, dict):
                continue
            public_manifest = manifests.get("public")
            if not isinstance(public_manifest, dict):
                continue
            size_str = public_manifest.get("size")
            if size_str is None:
                continue
            try:
                size_val = int(size_str)
            except (ValueError, TypeError):
                continue

            total += size_val
            found_any = True

        if found_any:
            return total
        else:
            last_error = f"attempt {attempt}: depots present but none had usable public manifest sizes (target_os={target_os})"
            log_error(appid, name, last_error)
            time.sleep(1)
            continue

    log_error(appid, name, f"giving up after {MAX_RETRIES} attempts. Last error: {last_error}")
    return None


def format_bytes(num_bytes):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"


class SteamSizeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Steam Library Size Calculator")
        self.geometry("760x580")
        self.resizable(True, True)

        self.results = []
        self.stop_requested = False

        self._build_input_frame()
        self._build_progress_frame()
        self._build_results_frame()

    def _build_input_frame(self):
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="x")

        ttk.Label(frame, text="Steam Web API Key:").grid(row=0, column=0, sticky="w")
        self.api_key_entry = ttk.Entry(frame, width=45, show="*")
        self.api_key_entry.grid(row=0, column=1, padx=5, pady=3)

        ttk.Label(frame, text="Steam URL Name (The Vanity Name):").grid(row=1, column=0, sticky="w")
        self.profile_entry = ttk.Entry(frame, width=45)
        self.profile_entry.grid(row=1, column=1, padx=5, pady=3)
        self.profile_entry.insert(0, "")

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=8)

        self.start_btn = ttk.Button(btn_frame, text="Start Scan", command=self.start_scan)
        self.start_btn.pack(side="left", padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side="left", padx=5)

        self.export_btn = ttk.Button(btn_frame, text="Export CSV", command=self.export_csv, state="disabled")
        self.export_btn.pack(side="left", padx=5)

        self.retry_btn = ttk.Button(btn_frame, text="Retry Unknowns", command=self.retry_unknowns, state="disabled")
        self.retry_btn.pack(side="left", padx=5)

    def _build_progress_frame(self):
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="x")

        self.status_label = ttk.Label(frame, text="Ready.")
        self.status_label.pack(anchor="w")

        self.progress = ttk.Progressbar(frame, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", pady=5)

        self.total_label = ttk.Label(frame, text="Total size so far: 0 B", font=("Sans", 11, "bold"))
        self.total_label.pack(anchor="w")

    def _build_results_frame(self):
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)

        columns = ("name", "appid", "size")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings")
        self.tree.heading("name", text="Game", command=lambda: self.sort_by("name"))
        self.tree.heading("appid", text="App ID", command=lambda: self.sort_by("appid"))
        self.tree.heading("size", text="Size", command=lambda: self.sort_by("size"))
        self.tree.column("name", width=380)
        self.tree.column("appid", width=100, anchor="center")
        self.tree.column("size", width=150, anchor="e")

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def sort_by(self, key):
        if key == "size":
            self.results.sort(key=lambda r: r[2] if isinstance(r[2], int) else -1, reverse=True)
        elif key == "appid":
            self.results.sort(key=lambda r: int(r[1]))
        else:
            self.results.sort(key=lambda r: r[0].lower())
        self.refresh_tree()

    def refresh_tree(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for name, appid, size in self.results:
            size_str = format_bytes(size) if isinstance(size, int) else "Unknown"
            self.tree.insert("", "end", values=(name, appid, size_str))

    def start_scan(self):
        if vdf is None:
            messagebox.showerror(
                "Missing dependency",
                "The 'vdf' Python library is required to parse steamcmd output.\n\n"
                "Install it with:\npip install vdf --break-system-packages",
            )
            return

        api_key = self.api_key_entry.get().strip()
        profile = self.profile_entry.get().strip()

        if not api_key or not profile:
            messagebox.showerror("Missing info", "Please enter both API key and profile.")
            return

        self.results = []
        self.stop_requested = False
        self.tree.delete(*self.tree.get_children())
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.export_btn.config(state="disabled")
        self.retry_btn.config(state="disabled")
        self.status_label.config(text="Resolving profile...")

        thread = threading.Thread(target=self.run_scan, args=(api_key, profile), daemon=True)
        thread.start()

    def stop_scan(self):
        self.stop_requested = True
        self.status_label.config(text="Stopping after current game...")

    def run_scan(self, api_key, profile):
        try:
            steamid = resolve_steamid(api_key, profile)
            self.update_status(f"Resolved SteamID64: {steamid}. Fetching game list...")
            games = get_owned_games(api_key, steamid)
        except Exception as e:
            self.update_status("Error.")
            messagebox.showerror("Error", str(e))
            self.finish_scan()
            return

        total = len(games)
        self.progress["maximum"] = total
        self.progress["value"] = 0

        cache = load_cache()

        for i, game in enumerate(games, 1):
            if self.stop_requested:
                break
            self.process_one_game(game, i, total, cache)

        self.update_status("Done." if not self.stop_requested else "Stopped early.")
        self.finish_scan()

    def process_one_game(self, game, i, total, cache):
        appid = str(game["appid"])
        name = game.get("name", f"App {appid}")
        self.update_status(f"[{i}/{total}] {name}")

        try:
            cached_val = cache.get(appid)
            if isinstance(cached_val, int):
                size = cached_val
            else:
                size = get_size_from_steamcmd(appid, name)
                if size == "NO_STEAMCMD":
                    self.update_status("ERROR: steamcmd not found on PATH.")
                    messagebox.showerror(
                        "steamcmd not found",
                        "steamcmd is not installed, not on your PATH, or STEAMCMD_PATH is set incorrectly.\n\n"
                        "Install it with:\nsudo add-apt-repository multiverse\n"
                        "sudo apt update\nsudo apt install steamcmd\n\n"
                        "Or edit the STEAMCMD_PATH constant near the top of this script "
                        "to point directly to the steamcmd executable.",
                    )
                    self.stop_requested = True
                    return
                if size == "NO_VDF_LIB":
                    self.update_status("ERROR: vdf library not installed.")
                    messagebox.showerror("Missing dependency", "pip install vdf --break-system-packages")
                    self.stop_requested = True
                    return
                cache[appid] = size
                save_cache(cache)
                time.sleep(0.3)
        except Exception as e:
            log_error(appid, name, f"unexpected exception: {e}")
            size = None

        self.results.append((name, appid, size))
        self.progress["value"] = i

        total_bytes = sum(s for _, _, s in self.results if isinstance(s, int))
        self.total_label.config(text=f"Total size so far: {format_bytes(total_bytes)}")
        self.refresh_tree()

        if i % AUTOSAVE_EVERY_N == 0:
            self._write_csv(AUTOSAVE_CSV)

    def update_status(self, text):
        self.status_label.config(text=text)

    def finish_scan(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.export_btn.config(state="normal" if self.results else "disabled")
        unknown_count = sum(1 for _, _, s in self.results if not isinstance(s, int))
        self.retry_btn.config(state="normal" if unknown_count else "disabled")
        if self.results:
            self._write_csv(AUTOSAVE_CSV)

    def retry_unknowns(self):
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.retry_btn.config(state="disabled")
        self.stop_requested = False
        thread = threading.Thread(target=self._retry_unknowns_worker, daemon=True)
        thread.start()

    def _retry_unknowns_worker(self):
        cache = load_cache()
        unknown_indices = [idx for idx, (_, _, s) in enumerate(self.results) if not isinstance(s, int)]
        total = len(unknown_indices)

        for count, idx in enumerate(unknown_indices, 1):
            if self.stop_requested:
                break
            name, appid, _ = self.results[idx]
            self.update_status(f"Retrying [{count}/{total}]: {name}")
            try:
                size = get_size_from_steamcmd(appid, name)
                if isinstance(size, int):
                    cache[appid] = size
                    save_cache(cache)
                    self.results[idx] = (name, appid, size)
            except Exception as e:
                log_error(appid, name, f"retry exception: {e}")
            self.refresh_tree()
            total_bytes = sum(s for _, _, s in self.results if isinstance(s, int))
            self.total_label.config(text=f"Total size so far: {format_bytes(total_bytes)}")

        self.update_status("Retry pass complete.")
        self.finish_scan()

    def export_csv(self):
        if not self.results:
            return
        path = "steam_library_sizes.csv"
        self._write_csv(path)
        messagebox.showinfo("Exported", f"Saved to {os.path.abspath(path)}")

    def _write_csv(self, path):
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Game", "AppID", "Size (bytes)", "Size (formatted)"])
            for name, appid, size in self.results:
                size_bytes = size if isinstance(size, int) else ""
                size_fmt = format_bytes(size) if isinstance(size, int) else "Unknown"
                writer.writerow([name, appid, size_bytes, size_fmt])


if __name__ == "__main__":
    app = SteamSizeApp()
    app.mainloop()
