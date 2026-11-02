"""ホームページ更新ボットの ON/OFF をタスクトレイから切り替えるツール。

  pythonw tray.py
"""
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont

BOT_DIR = Path(__file__).resolve().parent
TASK = "LINE-HomepageBot"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "LINE-HomepageBot-Tray"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
NO_WINDOW = subprocess.CREATE_NO_WINDOW


# ---------- ボット（タスクスケジューラ） ----------

def schtasks(*args):
    return subprocess.run(["schtasks", *args], capture_output=True, creationflags=NO_WINDOW)


def bot_state():
    """'on' / 'off' / 'missing'"""
    r = schtasks("/query", "/tn", TASK, "/xml", "ONE")
    if r.returncode != 0:
        return "missing"
    xml = r.stdout.decode("utf-16", errors="ignore") if r.stdout[:2] in (b"\xff\xfe", b"\xfe\xff") \
        else r.stdout.decode("mbcs", errors="ignore")
    settings = re.search(r"<Settings>.*?</Settings>", xml, re.S)
    if settings and re.search(r"<Enabled>false</Enabled>", settings.group(0)):
        return "off"
    return "on"


def set_bot(on):
    schtasks("/change", "/tn", TASK, "/enable" if on else "/disable")


def register_bot():
    subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(BOT_DIR / "register_task.ps1")],
                   creationflags=NO_WINDOW)


# ---------- ログイン時の自動起動 ----------

def autostart_command():
    return f'"{PYTHONW}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


# ---------- 表示 ----------

COLORS = {"on": (46, 160, 67), "off": (140, 140, 140), "missing": (200, 60, 60)}
LABELS = {"on": "ON", "off": "OFF", "missing": "未登録"}


def make_icon(state):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=COLORS[state])
    try:
        font = ImageFont.truetype("arialbd.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    d.text((32, 33), "HP", fill="white", font=font, anchor="mm")
    return img


def last_activity():
    log = BOT_DIR / "poller.log"
    if not log.exists():
        return ""
    with open(log, "rb") as f:
        f.seek(max(0, log.stat().st_size - 4000))
        lines = f.read().decode("utf-8", errors="ignore").splitlines()
    for line in reversed(lines):
        if line[:4].isdigit():
            return "最終処理 " + line[5:16]
    return ""


class Tray:
    def __init__(self):
        self.state = bot_state()
        self.icon = pystray.Icon("hpbot", make_icon(self.state), self.title(), self.menu())

    def title(self):
        extra = last_activity()
        return f"ホームページ更新ボット: {LABELS[self.state]}" + (f"\n{extra}" if extra else "")

    def refresh(self):
        state = bot_state()
        if state != self.state:
            self.state = state
            self.icon.icon = make_icon(state)
        self.icon.title = self.title()[:127]
        self.icon.update_menu()

    def menu(self):
        M = pystray.MenuItem
        return pystray.Menu(
            M(lambda _: f"ボット: {LABELS[self.state]}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            M("ボットを動かす（ON）", self.toggle_bot, checked=lambda _: self.state == "on",
              visible=lambda _: self.state != "missing"),
            M("ボットを登録する", self.register, visible=lambda _: self.state == "missing"),
            M("今すぐ LINE を確認", self.run_now, enabled=lambda _: self.state == "on"),
            M("Windows ログイン時にこのツールを起動", self.toggle_autostart,
              checked=lambda _: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            M("ログを開く", lambda: os.startfile(BOT_DIR / "poller.log")
              if (BOT_DIR / "poller.log").exists() else None),
            M("フォルダを開く", lambda: os.startfile(BOT_DIR)),
            pystray.Menu.SEPARATOR,
            M("終了", lambda icon: icon.stop()),
        )

    def toggle_bot(self):
        on = self.state != "on"
        set_bot(on)
        self.refresh()
        self.icon.notify("LINE からの更新を受け付けます。" if on else
                         "停止しました。この間に LINE で送った指示は、ON にしたときに処理されます。",
                         f"ホームページ更新ボット: {LABELS[self.state]}")

    def register(self):
        register_bot()
        self.refresh()

    def run_now(self):
        schtasks("/run", "/tn", TASK)
        self.icon.notify("LINE のメッセージを確認しています。", "ホームページ更新ボット")

    def toggle_autostart(self):
        set_autostart(not autostart_enabled())
        self.icon.update_menu()

    def watch(self):
        while True:
            time.sleep(30)
            try:
                self.refresh()
            except Exception:
                pass

    def run(self):
        threading.Thread(target=self.watch, daemon=True).start()
        self.icon.run()


def main():
    # 二重起動防止
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\LINE-HomepageBot-Tray")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    Tray().run()


if __name__ == "__main__":
    main()
