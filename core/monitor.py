# -*- coding: utf-8 -*-
"""多会话监控：轮询解密库，发现**所有**会话的新消息——不靠 OCR 盯着当前窗口。

数据源是 wx_refresh 刷出来的 tools/decrypted*/message_*.db。每轮：
  1. wx_refresh.refresh() 增量解密（message 库总是重解，拿到微信刚落盘的记录）
  2. 每张会话表查 local_id > 上次见过的最大值的行
  3. 有新消息 → 回调（显示名, [(who, text, name), ...]），who ∈ {me, her}

第一轮只记基线不回调——不然一启动就把全部历史当新消息轰炸分析链。
"""
from __future__ import annotations

import glob
import hashlib
import os
import sqlite3
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    _ROOT = os.path.dirname(sys.executable)
else:
    _ROOT = os.path.dirname(_HERE)
_TOOLS = os.path.join(_ROOT, "tools")

# 非文本类型的占位标签，跟查看器一致
_TYPE_LABEL = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]", 48: "[位置]"}


def _dec(blob) -> str:
    """message_content 可能是 zstd 压缩的 blob 或明文。"""
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    if blob[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard as zstd
            return zstd.ZstdDecompressor(max_window_size=2**27).decompress(blob).decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        return blob.decode("utf-8")
    except Exception:
        return ""


def _render(ltype: int, content) -> str:
    if ltype == 1:
        return _dec(content).strip()
    if ltype in _TYPE_LABEL:
        return _TYPE_LABEL[ltype]
    if ltype == 10000:  # 系统消息（撤回提示等）
        import re
        return re.sub(r"<[^>]+>", "", _dec(content)).strip()
    return f"[类型{ltype}]"


class _Source:
    """一个解密库目录：联系人映射 + 消息表 → 会话的反查。"""

    def __init__(self, base: str):
        self.base = base
        self.self_wxid = ""
        sw = os.path.join(base, "self_wxid.txt")
        if os.path.exists(sw):
            self.self_wxid = open(sw, encoding="utf-8").read().strip()
        c = sqlite3.connect(os.path.join(base, "contact.db"))
        try:
            # 显示名：备注 > 昵称 > 微信号；md5 反查表名
            self.names = {}
            self.h2u = {}
            for uname, remark, nick, alias in c.execute(
                    "SELECT username, remark, nick_name, alias FROM contact WHERE username != ''"):
                self.names[uname] = (remark or nick or alias or uname).strip()
                for cand in (uname, uname.lower()):
                    self.h2u.setdefault(hashlib.md5(cand.encode()).hexdigest(), uname)
        finally:
            c.close()
        # session.db 的显示名兜底（群名主要在 contact，但保险）
        sp = os.path.join(base, "session.db")
        if os.path.exists(sp):
            s = sqlite3.connect(sp)
            try:
                for uname, summary, ts in s.execute(
                        "SELECT username, summary, sort_timestamp FROM SessionTable"):
                    self.names.setdefault(uname, uname)
            finally:
                s.close()

    def display(self, uname: str) -> str:
        return self.names.get(uname) or uname


class Monitor:
    """后台轮询。new_cb(display_name, msgs) 在新消息时被调；log_cb(text) 打日志。"""

    def __init__(self, new_cb, log_cb=print, interval: float = 8.0):
        self.new_cb = new_cb
        self.log = log_cb
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: dict[str, int] = {}  # 表路径 -> 见过的最大 local_id
        self._baselined = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _bases(self) -> list[str]:
        out = []
        for pat in ("decrypted", "decrypted_*"):
            out += [d for d in sorted(glob.glob(os.path.join(_TOOLS, pat)))
                    if os.path.exists(os.path.join(d, "contact.db"))]
        return out

    def _poll_once(self):
        """一轮：解密刷新 → 扫所有库的新消息。返回 [(显示名, who, text, name)]。"""
        from core import wx_refresh
        try:
            summary = wx_refresh.refresh()
            self.log(f"[监控] {summary}")
        except Exception as e:
            self.log(f"[监控] 解密刷新失败：{e}")
            return []
        found = []
        for base in self._bases():
            found += self._scan(base)
        return found

    def _scan(self, base: str) -> list:
        src = _Source(base)
        out = []
        for mdb in sorted(glob.glob(os.path.join(base, "message_*.db"))):
            conn = sqlite3.connect(mdb)
            try:
                n2id = {rid: un for rid, un in conn.execute(
                    "SELECT rowid, user_name FROM Name2Id")}
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
                for t in tables:
                    uname = src.h2u.get(t[4:])
                    if not uname:
                        continue
                    key = f"{mdb}|{t}"
                    last = self._seen.get(key)
                    if last is None:  # 第一次见这张表：只记基线
                        row = conn.execute(f"SELECT MAX(local_id) FROM {t}").fetchone()
                        self._seen[key] = row[0] or 0
                        continue
                    rows = conn.execute(
                        f"SELECT local_id, real_sender_id, create_time, local_type, message_content "
                        f"FROM {t} WHERE local_id > ? ORDER BY local_id", (last,)).fetchall()
                    if not rows:
                        continue
                    self._seen[key] = rows[-1][0]
                    name = src.display(uname)
                    for lid, sid, ts, ltype, content in rows:
                        su = n2id.get(sid, "")
                        who = "me" if su and su == src.self_wxid else "her"
                        # 单聊里对方没 sender 名；群聊用发送者显示名
                        sender = src.display(su) if su and su != src.self_wxid else None
                        text = _render(ltype, content)
                        if text:
                            out.append((name, who, text, sender))
            except Exception:
                continue
            finally:
                conn.close()
        return out

    def _run(self):
        self.log("[监控] 多会话监控已启动，正在建基线…")
        try:
            self._poll_once()  # 第一轮只建基线
        except Exception as e:
            self.log(f"[监控] 基线失败：{e}")
        self._baselined = True
        self.log("[监控] 基线完成，开始盯所有会话")
        while not self._stop.wait(self.interval):
            try:
                for name, who, text, sender in self._poll_once():
                    self.new_cb(name, who, text, sender)
            except Exception as e:
                self.log(f"[监控] 这轮出错：{e}")


if __name__ == "__main__":
    # ponytail: 拿真库建基线 + 打一轮，不联网
    sys.path.insert(0, _ROOT)  # 直接跑本文件时 core 包不在 path
    m = Monitor(lambda *a: print("新消息:", a), interval=999)
    m._poll_once()
    print("基线表数:", len(m._seen))
    m._seen.clear()  # 清掉再扫一次 = 把最近消息当新的打出来，验证查询链
    rows = m._poll_once()
    for r in rows[-5:]:
        print(r[0], r[1], (r[2] or "")[:30])
    print("monitor ok")
