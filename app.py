"""Clip Maker window. Double-click 'Start Clip Maker.bat' to open this."""
import json
import os
import queue
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import pipeline
import requirements_reader

BASE = Path(__file__).resolve().parent
SETTINGS = BASE / "settings.json"

FIELDS = [
    # key, label, default, help
    ("url", "Video or stream link", "", "YouTube, Twitch VOD or Kick link to clip"),
    ("campaign", "Campaign name", "", "Used to organise your folders and tracker"),
    ("required_text", "Must-include caption text", "", "e.g. #CORE — added to every caption"),
    ("start", "Only clip from (optional)", "", "e.g. 1:30:00 — saves time on long streams"),
    ("end", "...to (optional)", "", "e.g. 2:15:00"),
    ("num_clips", "Number of clips", "5", ""),
    ("min_len", "Shortest clip (seconds)", "20", ""),
    ("max_len", "Longest clip (seconds)", "60", ""),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Clip Maker")
        self.geometry("680x900")
        self.minsize(580, 700)
        self.q = queue.Queue()
        self.vars = {}
        self.req_text = ""
        saved = self.load()

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        row = 0

        # --- requirements link
        ttk.Label(frm, text="Requirements link").grid(row=row, column=0, sticky="w", pady=3)
        rl = ttk.Frame(frm)
        rl.grid(row=row, column=1, sticky="ew")
        rl.columnconfigure(0, weight=1)
        self.vars["req_url"] = tk.StringVar(value=saved.get("req_url", ""))
        ttk.Entry(rl, textvariable=self.vars["req_url"]).grid(row=0, column=0, sticky="ew")
        self.load_btn = ttk.Button(rl, text="Load", command=self.load_requirements)
        self.load_btn.grid(row=0, column=1, padx=(6, 0))
        row += 1
        ttk.Label(frm, text="e.g. reachclipping.com/CORE — fills in tags, rules and length for you",
                  foreground="#777").grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Separator(frm).grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1

        for key, label, default, hint in FIELDS:
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=saved.get(key, default) if key != "url" else "")
            ttk.Entry(frm, textvariable=v).grid(row=row, column=1, sticky="ew", pady=3)
            self.vars[key] = v
            row += 1
            if hint:
                ttk.Label(frm, text=hint, foreground="#777").grid(row=row, column=1, sticky="w")
                row += 1

        ttk.Label(frm, text="Campaign rules (for AI)").grid(row=row, column=0, sticky="nw", pady=3)
        self.rules = tk.Text(frm, height=5, wrap="word")
        self.rules.grid(row=row, column=1, sticky="ew", pady=3)
        self.rules.insert("1.0", saved.get("rules", ""))
        row += 1
        ttk.Label(frm, text="Filled in by Load, or paste/type the rules yourself",
                  foreground="#777").grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(frm, text="Layout").grid(row=row, column=0, sticky="w", pady=3)
        self.vars["layout"] = tk.StringVar(value=saved.get("layout", "Auto"))
        ttk.Combobox(frm, textvariable=self.vars["layout"], state="readonly",
                     values=["Auto", "Follow face", "Blurred background"]).grid(row=row, column=1, sticky="ew")
        row += 1
        ttk.Label(frm, text="Auto = follow the face for podcasts/IRL, blurred background for gameplay",
                  foreground="#777").grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(frm, text="Caption accuracy").grid(row=row, column=0, sticky="w", pady=3)
        self.vars["model"] = tk.StringVar(value=saved.get("model", "base"))
        ttk.Combobox(frm, textvariable=self.vars["model"], state="readonly",
                     values=["base", "small", "medium"]).grid(row=row, column=1, sticky="ew")
        row += 1
        ttk.Label(frm, text="base = fast, small = better, medium = best but slow",
                  foreground="#777").grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(frm, text="Claude API key (optional)").grid(row=row, column=0, sticky="w", pady=3)
        self.vars["api_key"] = tk.StringVar(value=saved.get("api_key", ""))
        ttk.Entry(frm, textvariable=self.vars["api_key"], show="•").grid(row=row, column=1, sticky="ew")
        row += 1
        ttk.Label(frm, text="Smarter moment picking, captions and rule reading. Blank = free method.",
                  foreground="#777").grid(row=row, column=1, sticky="w")
        row += 1

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=2, pady=12, sticky="ew")
        self.go = ttk.Button(btns, text="Make clips", command=self.start)
        self.go.pack(side="left")
        ttk.Button(btns, text="Open clips folder",
                   command=lambda: self.open_path(BASE / "output")).pack(side="left", padx=8)
        ttk.Button(btns, text="Open tracker",
                   command=lambda: self.open_path(BASE / "output" / "tracker.csv")).pack(side="left")
        ttk.Button(btns, text="Posting settings", command=self.posting_settings).pack(side="right")
        self.post_btn = ttk.Button(btns, text="Post clips...", command=self.post_clips)
        self.post_btn.pack(side="right", padx=8)
        row += 1

        self.logbox = tk.Text(frm, height=12, wrap="word", state="disabled")
        self.logbox.grid(row=row, column=0, columnspan=2, sticky="nsew")
        frm.rowconfigure(row, weight=1)
        self.after(150, self.poll)

    # ---------- settings
    def load(self):
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def collect(self):
        s = {k: v.get().strip() for k, v in self.vars.items()}
        s["rules"] = self.rules.get("1.0", "end").strip()
        return s

    def save(self, s):
        keep = self.load()  # keep posting settings and anything else already saved
        keep.update({k: v for k, v in s.items() if k != "url"})
        SETTINGS.write_text(json.dumps(keep, indent=2), encoding="utf-8")

    # ---------- requirements
    def load_requirements(self):
        url = self.vars["req_url"].get().strip()
        if not url:
            messagebox.showwarning("Clip Maker", "Paste the campaign's requirements link first.")
            return
        key = self.vars["api_key"].get().strip()
        self.load_btn.config(state="disabled")
        threading.Thread(target=self._load_req_worker, args=(url, key), daemon=True).start()

    def _load_req_worker(self, url, key):
        try:
            data, text = requirements_reader.read_requirements(url, key, self.log)
            self.after(0, self._apply_requirements, data, text)
        except Exception as e:
            self.log(f"Couldn't read that page: {e}")
            self.after(0, lambda: self.load_btn.config(state="normal"))

    def _apply_requirements(self, data, text):
        self.load_btn.config(state="normal")
        if data.get("campaign") and not self.vars["campaign"].get().strip():
            self.vars["campaign"].set(data["campaign"])
        if data.get("required_text"):
            self.vars["required_text"].set(data["required_text"])
        if data.get("rules"):
            self.rules.delete("1.0", "end")
            self.rules.insert("1.0", data["rules"])
        if data.get("min_len"):
            self.vars["min_len"].set(str(int(data["min_len"])))
        if data.get("max_len"):
            self.vars["max_len"].set(str(int(data["max_len"])))

        campaign = self.vars["campaign"].get().strip() or "No campaign"
        folder = BASE / "output" / pipeline.slugify(campaign)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "requirements.txt").write_text(
            f"Source: {self.vars['req_url'].get().strip()}\n\n{text}", encoding="utf-8")

        self.log("Requirements loaded. Check the filled-in boxes before making clips.")
        lens = "not stated on the page" if not (data.get("min_len") or data.get("max_len")) else \
            f"{data.get('min_len') or '?'}-{data.get('max_len') or '?'}s"
        self.log(f"  Caption text: {data.get('required_text') or '(none found)'}   Clip length: {lens}")
        for w in data.get("warnings", []):
            self.log(f"  ! {w}")
        self.log(f"  Full requirements saved to output\\{pipeline.slugify(campaign)}\\requirements.txt")
        self.save(self.collect())

    # ---------- posting
    def posting_settings(self):
        s = self.load()
        win = tk.Toplevel(self)
        win.title("Posting settings")
        win.resizable(False, False)
        win.transient(self)
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both")
        ttk.Label(f, text="Clips are posted through Upload-Post (upload-post.com).\n"
                          "Make an account there, connect your social accounts to a profile,\n"
                          "then paste your API key and the profile name below.",
                  justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        key = tk.StringVar(value=s.get("up_key", ""))
        prof = tk.StringVar(value=s.get("up_profile", ""))
        ttk.Label(f, text="Upload-Post API key").grid(row=1, column=0, sticky="w")
        ttk.Entry(f, textvariable=key, show="•", width=40).grid(row=1, column=1, pady=3)
        ttk.Label(f, text="Profile name").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=prof, width=40).grid(row=2, column=1, pady=3)
        ttk.Label(f, text="Post to").grid(row=3, column=0, sticky="nw", pady=(8, 0))
        chosen = set(s.get("up_platforms", ["youtube", "tiktok", "instagram", "x"]))
        pv = {}
        box = ttk.Frame(f)
        box.grid(row=3, column=1, sticky="w", pady=(8, 0))
        for p, name in self.PLATFORM_NAMES.items():
            pv[p] = tk.BooleanVar(value=p in chosen)
            ttk.Checkbutton(box, text=name, variable=pv[p]).pack(anchor="w")

        def save():
            cur = self.load()
            cur.update(up_key=key.get().strip(), up_profile=prof.get().strip(),
                       up_platforms=[p for p, v in pv.items() if v.get()])
            SETTINGS.write_text(json.dumps(cur, indent=2), encoding="utf-8")
            win.destroy()
            self.log("Posting settings saved.")
        ttk.Button(f, text="Save", command=save).grid(row=4, column=1, sticky="e", pady=(12, 0))

    PLATFORM_NAMES = {"youtube": "YouTube Shorts", "tiktok": "TikTok", "instagram": "Instagram Reels", "x": "X"}

    def post_clips(self):
        from tkinter import filedialog
        import poster

        s = self.load()
        if not s.get("up_key") or not s.get("up_profile") or not s.get("up_platforms"):
            messagebox.showinfo("Post clips", "Set up posting first (Posting settings button).")
            self.posting_settings()
            return
        files = filedialog.askopenfilenames(
            title="Pick the clips to post", initialdir=str(BASE / "output"),
            filetypes=[("Video clips", "*.mp4")])
        if not files:
            return
        clips = [Path(f) for f in files]
        required = self.vars["required_text"].get().strip()
        lines, problems = [], []
        for c in clips:
            title, cap = poster.read_caption_file(c)
            miss = poster.missing_required(cap, required)
            lines.append(f"• {c.name}\n    {cap[:120] or '(no caption found)'}")
            if miss:
                problems.append(f"{c.name} is missing {' '.join(miss)}")
            if not cap:
                problems.append(f"{c.name} has no caption")
        plats = ", ".join(self.PLATFORM_NAMES[p] for p in s["up_platforms"])
        msg = (f"Post {len(clips)} clip(s) PUBLICLY to {plats}?\n\n" + "\n".join(lines[:8]) +
               ("\n..." if len(lines) > 8 else ""))
        if problems:
            msg += "\n\nWARNING:\n" + "\n".join(problems) + \
                   "\n(Edit the clip's .txt file to fix the caption, then post again.)"
        msg += "\n\nSubmit each link to the campaign within 30 minutes of posting."
        if not messagebox.askyesno("Post clips", msg, icon="warning" if problems else "question"):
            return
        self.post_btn.config(state="disabled")
        threading.Thread(target=self._post_worker, args=(clips, s), daemon=True).start()

    def _post_worker(self, clips, s):
        import poster
        try:
            for c in clips:
                title, cap = poster.read_caption_file(c)
                try:
                    links = poster.post_clip(s["up_key"], s["up_profile"], c, title, cap,
                                             s["up_platforms"], self.log)
                except Exception as e:
                    self.log(f"  {c.name}: {e}")
                    continue
                poster.update_tracker(BASE, c, links, self.log)
                for p, res in links.items():
                    name = self.PLATFORM_NAMES.get(p, p)
                    self.log(f"  {name}: {res['url'] or 'posted'}" if res["ok"] else f"  {name}: FAILED - {res['msg']}")
                self.log(f"  Links saved to {c.stem}_links.txt and the tracker. Submit them now!")
        except Exception as e:
            self.log(f"Posting problem: {e}")
            (BASE / "last_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        finally:
            self.after(0, lambda: self.post_btn.config(state="normal"))

    # ---------- actions
    def open_path(self, p: Path):
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix == ".csv" and not p.exists():
            messagebox.showinfo("Tracker", "No clips yet. The tracker appears after your first run.")
            return
        if not p.suffix:
            p.mkdir(exist_ok=True)
        os.startfile(p)  # Windows

    def log(self, msg):
        self.q.put(msg)

    def poll(self):
        while not self.q.empty():
            msg = self.q.get()
            if msg == "__DONE__":
                self.go.config(state="normal")
                continue
            self.logbox.config(state="normal")
            self.logbox.insert("end", msg + "\n")
            self.logbox.see("end")
            self.logbox.config(state="disabled")
        self.after(150, self.poll)

    def start(self):
        s = self.collect()
        if not s["url"]:
            messagebox.showwarning("Clip Maker", "Paste a video link first.")
            return
        try:
            int(s["num_clips"]); float(s["min_len"]); float(s["max_len"])
            pipeline.parse_time(s["start"]); pipeline.parse_time(s["end"])
        except ValueError:
            messagebox.showwarning("Clip Maker", "Check the numbers and times (use 1:30:00 style for times).")
            return
        self.save(s)
        self.go.config(state="disabled")
        self.log("Starting...")
        threading.Thread(target=self.work, args=(s,), daemon=True).start()

    def work(self, s):
        try:
            out = pipeline.run(s, BASE, self.log)
            self.log(f"Clips are in: {out}")
            os.startfile(out)
        except Exception as e:
            self.log(f"Problem: {e}")
            (BASE / "last_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            self.log("Full details saved to last_error.txt (you can send it to Claude for help).")
        finally:
            self.log("__DONE__")


if __name__ == "__main__":
    App().mainloop()
