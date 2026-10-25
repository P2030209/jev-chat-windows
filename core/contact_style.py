# -*- coding: utf-8 -*-
"""按人学习聊天风格：我跟每个人说话的方式不一样，起草回复要分开模仿。

数据来自 tools/ 下微信查看器已解密的库（tools/decrypted*/），不在采集链上
常驻解密——历史风格不需要实时，查看器导入过一次就够学。

用法：
    from core import contact_style
    style = contact_style.style_for("示例联系人")        # 有缓存直接给，没有返回 ""
    style = contact_style.learn("示例联系人", draft_llm)  # 拉历史 + LLM 蒸馏 + 缓存
"""
from __future__ import annotations

import glob
import hashlib
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    _ROOT = os.path.dirname(sys.executable)  # 打包后 config.json 和 tools/decrypted* 都在 exe 旁边
else:
    _ROOT = os.path.dirname(_HERE)
_CONFIG = os.path.join(_ROOT, "config.json")

# 每个人抽多少条「我发的」文本消息来学。30 条够看出句长/语气词/表情习惯，
# 又不会把 LLM 上下文撑爆
_SAMPLE = 30
# 蒸馏出来的风格摘要超过这么久（秒）就建议重学。风格变得慢，一个月一次足够
STALE_AFTER = 30 * 86400

_PROMPT = """下面是「我」发给某个人的最近 {n} 条微信消息（纯文本，时间倒序已纠正为正序）。
总结「我」跟这个人聊天时的说话风格，只写观察到的习惯，不要编造。覆盖：
句长和分段习惯、语气词/口头禅、标点和 emoji 使用、称呼方式、主动还是被动、
开玩笑的程度。最多 5 句短句，直接给结论，别解释。

消息：
{lines}"""


def _config() -> dict:
    try:
        import json
        with open(_CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_styles(styles: dict) -> None:
    import json
    cfg = _config()
    cfg["styles"] = styles
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


def style_for(contact: str) -> str:
    """这个联系人学过的风格摘要；没学过或学坏了返回 ""（调用方回退到全局手填 style）。"""
    if not contact:
        return ""
    entry = (_config().get("styles") or {}).get(contact) or {}
    return str(entry.get("style") or "")


def is_stale(contact: str) -> bool:
    """该重学了：没学过，或摘要太旧。"""
    entry = (_config().get("styles") or {}).get(contact) or {}
    return not entry.get("style") or time.time() - entry.get("updated", 0) > STALE_AFTER


def _sources() -> list[str]:
    """解密库目录。tools/decrypted* 是查看器导入的；_ROOT/decrypted* 是实时刷新解出来的。"""
    out = []
    for pat in ("decrypted", "decrypted_*"):
        out += [d for d in sorted(glob.glob(os.path.join(_ROOT, "tools", pat)))
                if os.path.exists(os.path.join(d, "contact.db"))]
    for pat in ("decrypted", "decrypted_*"):
        out += [d for d in sorted(glob.glob(os.path.join(_ROOT, pat)))
                if os.path.exists(os.path.join(d, "contact.db")) and d not in out]
    return out


def list_contacts() -> list[str]:
    """全部联系人的显示名（备注 > 昵称 > 微信号），群聊也列。给设置页「按人设关系」用。"""
    seen: dict[str, None] = {}
    for base in _sources():
        c = sqlite3.connect(os.path.join(base, "contact.db"))
        try:
            rows = c.execute(
                "SELECT username, remark, nick_name, alias FROM contact WHERE username != ''"
            ).fetchall()
        finally:
            c.close()
        for uname, remark, nick, alias in rows:
            name = (remark or nick or alias or uname or "").strip()
            if name:
                seen.setdefault(name)
    return sorted(seen)


def _find_user(base: str, contact: str) -> str | None:
    """按显示名（备注 > 昵称 > 微信号）反查 username。跟查看器一样以 remark 优先。"""
    c = sqlite3.connect(os.path.join(base, "contact.db"))
    try:
        rows = c.execute(
            "SELECT username, remark, nick_name, alias FROM contact WHERE username != ''"
        ).fetchall()
    finally:
        c.close()
    want = contact.strip().lower()
    # 先精确匹配（备注/昵称/别名/微信号），再退到包含匹配
    for keys in (lambda r: (r[1], r[2], r[3], r[0]),):
        for uname, remark, nick, alias in rows:
            if any((v or "").strip().lower() == want for v in keys((uname, remark, nick, alias))):
                return uname
    for uname, remark, nick, alias in rows:
        if any(want in (v or "").strip().lower() for v in (remark, nick) if v):
            return uname
    return None


def _my_texts(base: str, uname: str, limit: int) -> list[str]:
    """我在这个会话里发的最近 limit 条文本消息（正序）。读不出就空列表。
    微信 4.x 用 real_sender_id（Name2Id 的 rowid）标发送者；我的 wxid 在 self_wxid.txt。"""
    try:
        me = open(os.path.join(base, "self_wxid.txt"), encoding="utf-8").read().strip()
    except OSError:
        return []
    if not me:
        return []
    digest = hashlib.md5(uname.encode()).hexdigest()
    digest_l = hashlib.md5(uname.lower().encode()).hexdigest()
    for i in range(5):
        p = os.path.join(base, f"message_{i}.db")
        if not os.path.exists(p):
            continue
        conn = sqlite3.connect(p)
        try:
            n2id = {rid: un for rid, un in conn.execute("SELECT rowid, user_name FROM Name2Id")}
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")}
            table = f"Msg_{digest}" if f"Msg_{digest}" in tables else \
                f"Msg_{digest_l}" if f"Msg_{digest_l}" in tables else None
            if not table:
                continue
            # local_type=1 是文本；按 local_id 倒序拿最近再翻正。发送者在 Python 侧过滤
            # （real_sender_id 要过 Name2Id 映射，SQL 里 join 不如直接查快）
            rows = conn.execute(
                f"SELECT real_sender_id, message_content FROM {table} "
                f"WHERE local_type = 1 ORDER BY local_id DESC LIMIT ?", (limit * 4,)).fetchall()
            texts = []
            for sid, blob in rows:
                if n2id.get(sid) != me:
                    continue
                t = _decode(blob).strip()
                # 过滤太短的（哦/嗯学不到东西）和太长的（转发小作文不是风格）
                if 2 <= len(t) <= 200:
                    texts.append(t)
                if len(texts) >= limit:
                    break
            return texts[::-1]
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return []


def _decode(blob) -> str:
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    try:
        import io
        import zstandard as zstd
        if blob[:4] == b"\x28\xb5\x2f\xfd":
            dctx = zstd.ZstdDecompressor(max_window_size=2**27)
            try:
                return dctx.decompress(blob).decode("utf-8", "replace")
            except Exception:
                return dctx.stream_reader(io.BytesIO(blob)).read(1 << 20).decode("utf-8", "replace")
    except Exception:
        pass
    try:
        return blob.decode("utf-8")
    except Exception:
        return ""


def history(contact: str, limit: int = _SAMPLE) -> list[str]:
    """按会话显示名拿「我发的」历史文本。库没导入过 / 找不到人 / 没有消息都是空列表。"""
    for base in _sources():
        uname = _find_user(base, contact)
        if uname:
            texts = _my_texts(base, uname, limit)
            if texts:
                return texts
    return []


def learn(contact: str, draft, limit: int = _SAMPLE) -> str:
    """学这个人的：拉我发的历史 → 起草模型蒸馏 → 写进 config.json styles。

    draft 是 core.llm.complete 那样的可调用：draft(system_prompt, user_prompt) -> str。
    学不到（没库/没历史/LLM 失败）抛 RuntimeError，消息给人看。
    """
    texts = history(contact, limit)
    if len(texts) < 5:
        raise RuntimeError(
            f"拿不到「{contact}」的聊天记录（先在 tools 查看器里导入账号，或跟TA的聊天太少）")
    lines = "\n".join(f"- {t}" for t in texts)
    style = draft("你是聊天风格分析师。", _PROMPT.format(n=len(texts), lines=lines)).strip()
    if not style:
        raise RuntimeError("模型没给出风格摘要")
    styles = _config().get("styles") or {}
    styles[contact] = {"style": style, "updated": int(time.time()), "sample": len(texts)}
    _save_styles(styles)
    return style


if __name__ == "__main__":
    # ponytail: 纯文件/db 操作，不碰模型。造一个假的解密库结构跑一遍查询链。
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "tools", "decrypted")  # _sources() 拼的是 _ROOT/tools/decrypted*
        os.makedirs(base)
        c = sqlite3.connect(os.path.join(base, "contact.db"))
        c.execute("CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT, alias TEXT)")
        c.execute("INSERT INTO contact VALUES ('wxid_abc', '示例联系人', '肥', '')")
        c.commit()
        c.close()
        m = sqlite3.connect(os.path.join(base, "message_0.db"))
        digest = hashlib.md5("wxid_abc".encode()).hexdigest()
        m.execute("CREATE TABLE Name2Id (user_name TEXT)")
        m.execute("INSERT INTO Name2Id VALUES ('wxid_abc')")   # rowid=1 对方
        m.execute("INSERT INTO Name2Id VALUES ('wxid_me')")    # rowid=2 我
        m.execute(f"CREATE TABLE Msg_{digest} (message_content BLOB, local_type INT, "
                  f"real_sender_id INT, local_id INT)")
        for i, (txt, sid) in enumerate([("在吗", 1), ("在的，说吧", 2), ("晚上吃啥", 1),
                                        ("随便，你定", 2), ("行，那就火锅", 2),
                                        ("我下班了过去", 2), ("好", 1), ("嗯快到了", 2)]):
            m.execute(f"INSERT INTO Msg_{digest} VALUES (?, 1, ?, ?)", (txt, sid, 1000 + i))
        m.commit()
        m.close()
        with open(os.path.join(base, "self_wxid.txt"), "w", encoding="utf-8") as f:
            f.write("wxid_me")

        # 临时把 _ROOT 指到假的（_sources 拼的是 _ROOT/tools）
        cs = sys.modules[__name__]
        old_root, old_cfg = cs._ROOT, cs._CONFIG
        cs._ROOT = tmp
        cs._CONFIG = os.path.join(tmp, "config.json")
        try:
            assert cs._find_user(base, "示例联系人") == "wxid_abc"
            assert cs._find_user(base, "不存在的人") is None
            texts = cs.history("示例联系人")
            assert texts == ["在的，说吧", "随便，你定", "行，那就火锅", "我下班了过去", "嗯快到了"], texts
            # learn 用假 draft，验证缓存读写
            got = cs.learn("示例联系人", lambda sys, user: "话少，直给。")
            assert got == "话少，直给。"
            assert cs.style_for("示例联系人") == "话少，直给。"
            assert not cs.is_stale("示例联系人")
            assert cs.is_stale("没学过的人")
            # 学不到要抛错
            try:
                cs.learn("不存在的人", lambda s, u: "x")
                raise AssertionError("该抛 RuntimeError")
            except RuntimeError:
                pass
        finally:
            cs._ROOT, cs._CONFIG = old_root, old_cfg
    print("contact_style ok")
