"""中継スクリプト(GAS)・LINE・WordPress との通信。"""
import mimetypes
from pathlib import Path

import requests


class Gas:
    def __init__(self, url, secret):
        self.url = url
        self.secret = secret

    def _check(self, r):
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"中継スクリプトのエラー: {data['error']}")
        return data

    def fetch(self):
        r = requests.get(self.url, params={"key": self.secret, "action": "fetch"}, timeout=60)
        return self._check(r)["rows"]

    def ack(self, ids):
        r = requests.post(self.url, params={"key": self.secret, "action": "ack"}, json={"ids": ids}, timeout=60)
        self._check(r)

    def save_preview(self, token, title, html):
        r = requests.post(self.url, params={"key": self.secret, "action": "preview"},
                          json={"token": token, "title": title, "html": html}, timeout=60)
        self._check(r)

    def load_state(self):
        """保存された処理状態。中継スクリプトが古い（未対応）か、まだ無ければ None。"""
        r = requests.get(self.url, params={"key": self.secret, "action": "state"}, timeout=60)
        return self._check(r).get("state")

    def save_state(self, state):
        r = requests.post(self.url, params={"key": self.secret, "action": "state"}, json=state, timeout=60)
        self._check(r)

    def preview_url(self, token):
        return f"{self.url}?preview={token}"


class Line:
    API = "https://api.line.me/v2/bot"
    DATA = "https://api-data.line.me/v2/bot"

    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}"}

    def push(self, to, messages):
        r = requests.post(f"{self.API}/message/push", headers=self.headers,
                          json={"to": to, "messages": messages}, timeout=30)
        r.raise_for_status()

    def push_text(self, to, text):
        self.push(to, [{"type": "text", "text": text[:5000]}])

    def transcoding_status(self, message_id):
        """動画の変換状況: processing / succeeded / failed"""
        r = requests.get(f"{self.DATA}/message/{message_id}/content/transcoding", headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json()["status"]

    def download(self, message_id, dest_without_ext):
        """メッセージの画像・動画を保存し、保存先のパスを返す。"""
        r = requests.get(f"{self.DATA}/message/{message_id}/content", headers=self.headers, timeout=300, stream=True)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "").split(";")[0]
        ext = {"image/jpeg": ".jpg", "video/mp4": ".mp4"}.get(ctype) or mimetypes.guess_extension(ctype) or ".bin"
        path = Path(f"{dest_without_ext}{ext}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
        return path

    def bot_info(self):
        r = requests.get(f"{self.API}/info", headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json()


class WordPress:
    """LINE 専用ユーザー（権限: 投稿者）として操作する。自分の記事以外は触らない。"""

    def __init__(self, url, user, app_password):
        self.api = url.rstrip("/") + "/wp-json/wp/v2"
        self.s = requests.Session()
        self.s.auth = (user, app_password)
        self._me = None

    def _req(self, method, path, **kw):
        r = self.s.request(method, self.api + path, timeout=300, **kw)
        if not r.ok:
            try:
                detail = r.json().get("message", r.text)
            except ValueError:
                detail = r.text
            raise RuntimeError(f"WordPress {r.status_code}: {detail[:300]}")
        return r.json()

    def me_id(self):
        if self._me is None:
            self._me = self._req("GET", "/users/me")["id"]
        return self._me

    def categories(self):
        return [{"id": c["id"], "name": c["name"]}
                for c in self._req("GET", "/categories", params={"per_page": 100})]

    def my_posts(self, n=10):
        return self._req("GET", "/posts", params={
            "author": self.me_id(), "status": "draft,pending,publish,private",
            "context": "edit", "per_page": n, "orderby": "modified"})

    def get_my_post(self, post_id):
        try:
            post = self._req("GET", f"/posts/{post_id}", params={"context": "edit"})
        except RuntimeError as e:
            if "WordPress 403" in str(e):  # 投稿者権限では他人の記事は WordPress 側で拒否される
                raise PermissionError(f"記事 {post_id} は LINE から投稿した記事ではないため変更できません。")
            raise
        if post["author"] != self.me_id():
            raise PermissionError(f"記事 {post_id} は LINE から投稿した記事ではないため変更できません。")
        return post

    def create_post(self, fields):
        return self._req("POST", "/posts", json=fields)

    def update_my_post(self, post_id, fields):
        self.get_my_post(post_id)
        return self._req("POST", f"/posts/{post_id}", json=fields)

    def trash_my_post(self, post_id):
        """ゴミ箱へ移す（WordPress 管理画面から30日間は復元できる）。"""
        self.get_my_post(post_id)
        return self._req("DELETE", f"/posts/{post_id}")

    def delete_media(self, media_id):
        return self._req("DELETE", f"/media/{media_id}", params={"force": "true"})

    def upload(self, path, filename):
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            return self._req("POST", "/media", data=f, headers={
                "Content-Type": mime,
                "Content-Disposition": f'attachment; filename="{filename}"'})
