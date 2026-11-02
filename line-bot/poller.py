"""LINE → Claude → WordPress 連携。

PC ではタスクスケジューラから1分ごとに、クラウドでは GitHub Actions から LINE 受信のたびに実行される。
  python poller.py          … 溜まった LINE メッセージを処理する
  python poller.py --check  … 設定と接続を確認する

config.json が無いときは環境変数（GAS_URL など）から設定を読む（GitHub Actions 用）。
"""
import datetime
import html
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from clients import Gas, Line, WordPress

BOT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BOT_DIR / "config.json"
STATE_FILE = BOT_DIR / "state.json"
LOCK_FILE = BOT_DIR / "poller.lock"
PROMPT_FILE = BOT_DIR / "prompt.md"
INBOX = BOT_DIR / "inbox"

MEDIA_RE = re.compile(r"\{\{media:(\d+)\}\}")
IN_CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))

_handlers = [logging.FileHandler(BOT_DIR / "poller.log", encoding="utf-8")]
if IN_CLOUD:
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(handlers=_handlers, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poller")


class Deferred(Exception):
    """まだ処理できない（動画の変換待ちなど）。次回に回す。"""


# ---------- 状態・ロック ----------

def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    env = os.environ
    return {"gas_url": env["GAS_URL"], "gas_secret": env["GAS_SECRET"],
            "line_channel_access_token": env["LINE_CHANNEL_ACCESS_TOKEN"],
            "wp_url": env["WP_URL"], "wp_user": env["WP_USER"], "wp_app_password": env["WP_APP_PASSWORD"],
            "claude_model": env.get("CLAUDE_MODEL", "")}


def load_state(gas):
    """状態は中継スクリプトのシートに置き、PC とクラウドで共有する。
    中継スクリプトが古い（状態の保存に未対応）か、まだ空のときは state.json を使う。"""
    state = gas.load_state()
    if not state and STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state = state or {}
    for key, default in (("seen", []), ("pending_media", {}), ("current_post", {}), ("pending", {}), ("owners", {})):
        state.setdefault(key, default)
    # 作成者の記録を始める前の記事は、作業中だった人を作成者とみなす
    for sender, post_id in state["current_post"].items():
        state["owners"].setdefault(str(post_id), line_user(sender))
    return state


def line_user(sender):
    """グループ内の発言でも、送った人本人の LINE ユーザーID を返す。"""
    return sender.split("@", 1)[0]


def save_state(state, gas):
    state["seen"] = state["seen"][-500:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)
    gas.save_state(state)


def acquire_lock():
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime > 3600:
        LOCK_FILE.unlink()  # 前回の異常終了で残ったもの
    try:
        os.close(os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL))
        return True
    except FileExistsError:
        return False


# ---------- 本体 ----------

class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.gas = Gas(cfg["gas_url"], cfg["gas_secret"])
        self.line = Line(cfg["line_channel_access_token"])
        self.wp = WordPress(cfg["wp_url"], cfg["wp_user"], cfg["wp_app_password"])
        self.state = load_state(self.gas)

    def run(self):
        """溜まったメッセージを処理する。後回しにしたものがあれば True を返す。"""
        blocked = set()
        for row in self.gas.fetch():
            ev = row["event"]
            src = ev["source"]
            sender = src["userId"] + (f"@{src.get('groupId') or src.get('roomId')}" if src["type"] != "user" else "")
            reply_to = src.get("groupId") or src.get("roomId") or src["userId"]
            if sender in blocked:
                continue  # 同じ人の前のメッセージが処理待ちなので順番を守る
            eid = ev.get("webhookEventId")
            if eid not in self.state["seen"]:
                try:
                    self.handle(ev, sender, reply_to)
                except Deferred:
                    blocked.add(sender)
                    continue
                except Exception as e:
                    log.exception("処理に失敗しました")
                    self.notify(reply_to, f"エラーが発生しました。\n{str(e)[:300]}")
                if eid:
                    self.state["seen"].append(eid)
                save_state(self.state, self.gas)
            self.gas.ack([row["id"]])
        self.cleanup_inbox()
        save_state(self.state, self.gas)
        return bool(blocked)

    def notify(self, to, text):
        try:
            self.line.push_text(to, text)
        except Exception:
            log.exception("LINE への送信に失敗しました")

    def handle(self, ev, sender, reply_to):
        if ev["type"] == "postback":
            data = ev["postback"]["data"]
            if data.startswith("approve:"):
                self.approve(reply_to, data.split(":", 1)[1])
            elif data.startswith("cancel:"):
                self.cancel(sender, reply_to, data.split(":", 1)[1])
            elif data.startswith("delete:"):
                self.delete_post(sender, reply_to, int(data.split(":", 1)[1]))
            elif data == "delete_abort":
                self.notify(reply_to, "削除をやめました。")
            return
        msg = ev["message"]
        if msg["type"] in ("image", "video"):
            self.save_media(ev, msg, sender, reply_to)
        elif msg["type"] == "text":
            text = msg["text"]
            prefix = self.cfg.get("group_prefix", "#HP")
            if ev["source"]["type"] != "user" and text.startswith(prefix):
                text = text[len(prefix):].strip()
            log.info("指示 %s: %s", sender, text)
            self.instruction(sender, reply_to, text)

    # ----- 写真・動画 -----

    def save_media(self, ev, msg, sender, reply_to):
        if msg["type"] == "video":
            status = self.line.transcoding_status(msg["id"])
            if status == "processing":
                if time.time() - ev["timestamp"] / 1000 < 1800:
                    raise Deferred()
                status = "failed"
            if status == "failed":
                self.notify(reply_to, "動画を受け取れませんでした。もう一度送ってください。")
                return
        stamp = datetime.datetime.fromtimestamp(ev["timestamp"] / 1000).strftime("%Y%m%d_%H%M%S")
        path = self.line.download(msg["id"], INBOX / f"{stamp}_{msg['id']}")
        limit = self.cfg.get("max_upload_mb", 50)
        if path.stat().st_size > limit * 1024 * 1024:
            path.unlink()
            self.notify(reply_to, f"ファイルが大きすぎます（上限 {limit}MB）。動画は短くするか、YouTube のURLを送ってください。")
            return
        self.state["pending_media"].setdefault(sender, []).append(
            {"path": str(path.relative_to(BOT_DIR)), "type": msg["type"], "ts": ev["timestamp"] / 1000, "id": msg["id"]})

    def fresh_media(self, sender):
        window = self.cfg.get("media_window_minutes", 60) * 60
        media = [m for m in self.state["pending_media"].get(sender, []) if time.time() - m["ts"] < window]
        self.state["pending_media"][sender] = media
        for m in media:
            # クラウドでは実行のたびにファイルが消えるので、LINE から取り直す
            path = BOT_DIR / m["path"]
            if not path.exists():
                self.line.download(m["id"], path.with_suffix(""))
        return media

    def cleanup_inbox(self):
        if not INBOX.exists():
            return
        for p in INBOX.iterdir():
            if time.time() - p.stat().st_mtime > 7 * 86400:
                p.unlink()

    # ----- 指示の処理 -----

    def instruction(self, sender, reply_to, text):
        media = self.fresh_media(sender)
        current = self.state["current_post"].get(sender)
        posts = self.wp.my_posts(10)
        cats = self.wp.categories()
        focus = [current] if current else []

        for _ in range(3):
            prompt = self.build_prompt(text, media, posts, cats, current, focus, line_user(sender))
            d = ask_claude(self.cfg, prompt)
            log.info("Claude の判断: %s", {k: v for k, v in d.items() if k != "content"})
            if d["action"] == "need_post" and int(d["post_id"]) not in focus:
                focus.append(int(d["post_id"]))
                continue
            break

        action = d["action"]
        if action in ("create", "update"):
            self.apply_change(sender, reply_to, d, media)
        elif action == "publish":
            self.notify(reply_to, d.get("message") or "公開します。")
            self.publish_post(reply_to, int(d.get("post_id") or current))
        elif action == "cancel" and (d.get("post_id") or current):
            self.cancel_post(sender, reply_to, int(d.get("post_id") or current))
        elif action == "delete" and d.get("post_id"):
            self.confirm_delete(sender, reply_to, int(d["post_id"]))
        elif action == "list":
            self.list_posts(sender, reply_to)
        else:
            self.notify(reply_to, d.get("message") or "指示を読み取れませんでした。もう少し詳しく教えてください。")

    def post_for_context(self, post_id):
        """修正の材料になる記事本文。公開済み記事の未承認の修正があればそちらを渡す。"""
        try:
            p = self.wp.get_my_post(post_id)
        except Exception as e:
            return {"id": post_id, "error": str(e)}
        title, content = p["title"]["raw"], p["content"]["raw"]
        for pend in self.state["pending"].values():
            if pend["post_id"] == post_id and pend["fields"]:
                title, content = pend["fields"]["title"], pend["fields"]["content"]
        return {"id": post_id, "status": p["status"], "title": title, "content": content}

    def build_prompt(self, text, media, posts, cats, current, focus, user):
        lines = [PROMPT_FILE.read_text(encoding="utf-8"), "",
                 "---", "", f"## 今日の日付\n{datetime.date.today():%Y年%m月%d日}", "",
                 f"## LINE からの指示\n{text}", "", "## 添付メディア"]
        if media:
            for i, m in enumerate(media, 1):
                kind = "画像" if m["type"] == "image" else "動画（中身は見られません）"
                lines.append(f"{i}. {kind}: {m['path']}")
        else:
            lines.append("なし")
        lines += ["", f"## 作業中の記事\n{current or 'なし'}", "", "## 編集できる記事一覧（LINE から投稿した記事・新しい順）"]
        status_ja = {"draft": "下書き", "publish": "公開中", "pending": "承認待ち", "private": "非公開"}
        for p in posts:
            mine = "、指示した人が作成" if self.state["owners"].get(str(p["id"])) == user else ""
            lines.append(f"- id {p['id']}: {p['title']['raw']}（{status_ja.get(p['status'], p['status'])}、{p['modified'][:10]}{mine}）")
        if not posts:
            lines.append("なし")
        lines += ["", "## 本文を確認済みの記事"]
        for pid in focus:
            lines.append("```json\n" + json.dumps(self.post_for_context(pid), ensure_ascii=False, indent=1) + "\n```")
        if not focus:
            lines.append("なし")
        lines += ["", "## カテゴリー一覧", json.dumps(cats, ensure_ascii=False)]
        return "\n".join(lines)

    def apply_change(self, sender, reply_to, d, media):
        if not d.get("title") or not d.get("content"):
            raise RuntimeError("記事のタイトルか本文が作れませんでした。")
        content = d["content"]
        used = {int(n) for n in MEDIA_RE.findall(content)}
        featured = d.get("featured_media")
        if isinstance(featured, int):
            used.add(featured)

        uploaded = {}
        for n in sorted(used):
            if 1 <= n <= len(media):
                m = media[n - 1]
                path = BOT_DIR / m["path"]
                uploaded[n] = self.wp.upload(path, f"line_{m['id']}{path.suffix}")

        def embed(match):
            u = uploaded.get(int(match.group(1)))
            if not u:
                return ""
            if u["media_type"] == "image":
                return f'<figure class="wp-block-image size-large"><img src="{u["source_url"]}" alt="" class="wp-image-{u["id"]}"/></figure>'
            return f'<figure class="wp-block-video"><video controls src="{u["source_url"]}"></video></figure>'

        fields = {"title": d["title"], "content": MEDIA_RE.sub(embed, content)}
        valid_cats = [c for c in d.get("categories") or [] if isinstance(c, int)]
        if valid_cats:
            fields["categories"] = valid_cats
        if featured in uploaded and uploaded[featured]["media_type"] == "image":
            fields["featured_media"] = uploaded[featured]["id"]

        if d["action"] == "create":
            post_id = self.wp.create_post({**fields, "status": "draft"})["id"]
            self.state["owners"][str(post_id)] = line_user(sender)
            hold = None
        else:
            post_id = int(d["post_id"])
            if self.wp.get_my_post(post_id)["status"] == "publish":
                hold = fields  # 公開中の記事は OK が出るまで反映しない
            else:
                self.wp.update_my_post(post_id, fields)
                hold = None

        token = secrets.token_urlsafe(16)
        self.gas.save_preview(token, fields["title"], render_preview(fields["title"], fields["content"]))
        # 取り消したときに消せるよう、公開前にアップロードした写真・動画を覚えておく
        media_ids = [i for p in self.state["pending"].values() if p["post_id"] == post_id for i in p.get("media", [])]
        media_ids += [u["id"] for u in uploaded.values()]
        self.state["pending"] = {t: p for t, p in self.state["pending"].items() if p["post_id"] != post_id}
        self.state["pending"][token] = {"post_id": post_id, "fields": hold, "media": media_ids, "created": time.time()}
        self.state["current_post"][sender] = post_id
        self.state["pending_media"][sender] = []

        what = "公開中の記事の修正案" if hold else "下書き"
        self.line.push(reply_to, [
            {"type": "text", "text": (d.get("message") or "")[:4000] or f"{what}を作りました。"},
            {"type": "template", "altText": f"プレビュー: {fields['title']}"[:400], "template": {
                "type": "buttons",
                "text": f"「{fields['title'][:60]}」の{what}です。\nプレビューを確認して、よければ「公開する」を押してください。"[:160],
                "actions": [
                    {"type": "uri", "label": "プレビューを見る", "uri": self.gas.preview_url(token)},
                    {"type": "postback", "label": "OK・公開する", "data": f"approve:{token}",
                     "displayText": "OK、公開してください"},
                    {"type": "postback", "label": "修正を取り消す" if hold else "公開しない（下書き削除）",
                     "data": f"cancel:{token}",
                     "displayText": "修正を取り消してください" if hold else "公開をやめて、下書きを消してください"},
                ]}},
        ])

    # ----- 公開 -----

    def approve(self, reply_to, token):
        pend = self.state["pending"].get(token)
        if not pend:
            self.notify(reply_to, "このプレビューは古いか、すでに処理済みです。最新のプレビューのボタンを押してください。")
            return
        self.publish_post(reply_to, pend["post_id"])

    def publish_post(self, reply_to, post_id):
        fields = {}
        for t, pend in list(self.state["pending"].items()):
            if pend["post_id"] == post_id:
                fields = dict(pend["fields"] or {})
                del self.state["pending"][t]
        fields["status"] = "publish"
        post = self.wp.update_my_post(post_id, fields)
        self.notify(reply_to, f"公開しました！\n{html.unescape(post['title']['rendered'])}\n{post['link']}")

    # ----- 取り消し -----

    def cancel(self, sender, reply_to, token):
        pend = self.state["pending"].get(token)
        if not pend:
            self.notify(reply_to, "このプレビューは古いか、すでに処理済みです。最新のプレビューのボタンを押してください。")
            return
        self.cancel_post(sender, reply_to, pend["post_id"])

    def cancel_post(self, sender, reply_to, post_id):
        """下書きならゴミ箱へ移す。公開中の記事なら未承認の修正だけを捨てる。"""
        post = self.wp.get_my_post(post_id)
        title = post["title"]["raw"]
        if post["status"] == "draft":
            if not self.check_owner(sender, reply_to, post_id, title):
                return
            self.discard_pending(post_id)
            self.trash(post_id)
            text = f"「{title}」の公開を取りやめ、下書きを削除しました。"
        else:
            self.discard_pending(post_id)
            text = f"「{title}」の修正を取り消しました。ホームページの記事はそのままです。"
        if self.state["current_post"].get(sender) == post_id:
            del self.state["current_post"][sender]
        self.notify(reply_to, text)

    def discard_pending(self, post_id):
        """未承認の修正を捨て、そのためにアップロードした写真・動画も消す。"""
        for t, pend in list(self.state["pending"].items()):
            if pend["post_id"] != post_id:
                continue
            for mid in pend.get("media", []):
                try:
                    self.wp.delete_media(mid)
                except Exception:
                    log.exception("写真・動画 %s の削除に失敗しました", mid)
            del self.state["pending"][t]

    def trash(self, post_id):
        self.wp.trash_my_post(post_id)
        self.state["owners"].pop(str(post_id), None)
        for s, pid in list(self.state["current_post"].items()):
            if pid == post_id:
                del self.state["current_post"][s]

    def check_owner(self, sender, reply_to, post_id, title):
        if self.state["owners"].get(str(post_id)) == line_user(sender):
            return True
        self.notify(reply_to, f"「{title}」は記事を作成した人だけが削除できます。")
        return False

    # ----- 削除・一覧 -----

    def confirm_delete(self, sender, reply_to, post_id):
        title = self.wp.get_my_post(post_id)["title"]["raw"]
        if not self.check_owner(sender, reply_to, post_id, title):
            return
        self.line.push(reply_to, [{"type": "template", "altText": f"削除の確認: {title}"[:400], "template": {
            "type": "confirm",
            "text": f"「{title[:60]}」を削除しますか？\n公開中の場合はホームページからも消えます。"[:240],
            "actions": [
                {"type": "postback", "label": "削除する", "data": f"delete:{post_id}",
                 "displayText": "削除してください"},
                {"type": "postback", "label": "やめる", "data": "delete_abort", "displayText": "やめます"},
            ]}}])

    def delete_post(self, sender, reply_to, post_id):
        try:
            post = self.wp.get_my_post(post_id)
        except RuntimeError as e:
            if "WordPress 404" not in str(e) and "WordPress 410" not in str(e):
                raise
            post = {"status": "trash"}
        if post["status"] == "trash":
            self.notify(reply_to, "この記事はすでに削除されています。")
            return
        title = post["title"]["raw"]
        if not self.check_owner(sender, reply_to, post_id, title):
            return
        self.discard_pending(post_id)
        self.trash(post_id)
        self.notify(reply_to, f"「{title}」を削除しました。")

    def list_posts(self, sender, reply_to):
        user = line_user(sender)
        status_ja = {"draft": "下書き", "publish": "公開中", "pending": "承認待ち", "private": "非公開"}
        mine = [p for p in self.wp.my_posts(100) if self.state["owners"].get(str(p["id"])) == user]
        if not mine:
            self.notify(reply_to, "あなたが LINE から作成した記事はありません。")
            return
        lines = [f"あなたが作成した記事（{len(mine)}件・新しく更新した順）"]
        for i, p in enumerate(mine, 1):
            lines.append(f"\n{i}. {p['title']['raw']}\n（{status_ja.get(p['status'], p['status'])}・{p['modified'][:10]}）")
            if p["status"] == "publish":
                lines.append(p["link"])
        lines.append("\n修正・削除したいときは「〇〇の記事を削除して」のように記事名を添えて送ってください。")
        self.notify(reply_to, "\n".join(lines))


def render_preview(title, content):
    return f"""<style>
body{{font-family:-apple-system,"Hiragino Sans","Yu Gothic",sans-serif;line-height:1.8;color:#333;margin:0;background:#fafafa}}
.note{{background:#fff3cd;color:#664d03;padding:8px 16px;font-size:13px}}
main{{max-width:720px;margin:0 auto;padding:16px;background:#fff}}
h1{{font-size:1.5em;line-height:1.4}} img,video{{max-width:100%;height:auto}} figure{{margin:1em 0}}
</style>
<div class="note">プレビュー（実際のホームページとは文字や色の見た目が少し異なります）</div>
<main><h1>{html.escape(title)}</h1>{content}</main>"""


def ask_claude(cfg, prompt):
    cmd = [shutil.which(cfg.get("claude_command", "claude")) or cfg.get("claude_command", "claude"),
           "-p", "--output-format", "json", "--tools", "Read", "--allowedTools", "Read",
           "--no-session-persistence"]
    if cfg.get("claude_model"):
        cmd += ["--model", cfg["claude_model"]]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=BOT_DIR,
                       timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(f"Claude の実行に失敗しました: {(r.stderr or r.stdout)[-300:]}")
    out = json.loads(r.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"Claude のエラー: {str(out.get('result'))[:300]}")
    return extract_json(out["result"])


def extract_json(text):
    m = re.search(r"```json\s*(\{.*\})\s*```", text, re.S)
    if not m:
        m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Claude の返答を読み取れませんでした。")
    return json.loads(m.group(1) if m.lastindex else m.group(0))


def check(cfg):
    app = App(cfg)
    print("中継スクリプト: 未処理", len(app.gas.fetch()), "件")
    print("LINE ボット:", app.line.bot_info().get("displayName"))
    print("WordPress ユーザーID:", app.wp.me_id(), "/ カテゴリー:", [c["name"] for c in app.wp.categories()])
    print("Claude:", ask_claude(cfg, '```json\n{"ok": true}\n``` とだけ返してください。'))


def main():
    cfg = load_config()
    if "--check" in sys.argv:
        check(cfg)
        return
    if not acquire_lock():
        return
    try:
        # クラウドは次の起動が無いので、動画の変換待ちはその場で待つ（最大30分）
        for _ in range(30):
            if not App(cfg).run() or not IN_CLOUD:
                break
            time.sleep(60)
    except Exception:
        log.exception("実行に失敗しました")
        if IN_CLOUD:
            raise
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
