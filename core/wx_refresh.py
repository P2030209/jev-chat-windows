# -*- coding: utf-8 -*-
"""微信实时库 → 解密刷新。学聊天风格的数据源（core/contact_style.py 读 tools/decrypted*）。

解密本身复用 tools/wx_extract.py（查看器同一套）：MMKV 文件毫秒级拿 dbKey，
不需要管理员，不需要扫内存。这里只做三件事：
  1. 找最近活跃的微信账号
  2. auto_key 拿 dbKey
  3. 增量解密：message_*.db 总是重解（要最新记录）；contact/session 解过就跳过（变得慢）

为什么不用 wx_extract.extract_account 全量解：它把聊天图片/视频/文件库也全解了，
切换会话时等不起。风格学习只需要文本消息和联系人表。
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # 打包后：解密输出写到 exe 旁边的 tools/（可写）；wx_extract.py 被 spec 收进 _MEIPASS/tools
    _ROOT = os.path.dirname(sys.executable)
    _TOOLS_SRC = os.path.join(sys._MEIPASS, "tools")
else:
    _ROOT = os.path.dirname(_HERE)
    _TOOLS_SRC = os.path.join(_ROOT, "tools")
_TOOLS = os.path.join(_ROOT, "tools")

# contact/session 超过这么久才重解一次（秒）。联系人变化慢，没必要每次切会话都解
_META_FRESH = 86400


def _wx_extract():
    if _TOOLS_SRC not in sys.path:
        sys.path.insert(0, _TOOLS_SRC)
    import wx_extract  # tools/wx_extract.py，不是包，只能临时加 path
    return wx_extract


def _outdir_for(stem: str) -> str:
    """库文件名 stem → 输出目录。跟 wx_extract.extract_account 同一套约定：
    message_0 → decrypted/，message_0-副号 → decrypted_副号/（多账号按会话后缀分桶）。"""
    if "-" in stem:
        suffix = stem.rsplit("-", 1)[1]
        return os.path.join(_TOOLS, "decrypted_" + suffix)
    return os.path.join(_TOOLS, "decrypted")


def refresh(progress=None) -> str:
    """解密最新库到 tools/decrypted*/。progress(text) 可选，给人看进度。
    返回摘要给人看；失败抛 RuntimeError，消息能直接显示。

    微信必须在本机登录着（dbKey 从微信自己写的 MMKV 配置里读），全程本机操作，不上传。
    """
    wx = _wx_extract()
    accounts = wx.find_accounts()
    if not accounts:
        raise RuntimeError("没找到微信账号目录——需要本机登录过微信 4.x")
    acc = max(accounts, key=os.path.getmtime)
    who = os.path.basename(os.path.normpath(acc))

    if progress:
        progress(f"账号 {who}，正在取 dbKey…")
    key = wx.auto_key(acc)
    if not key:
        raise RuntimeError("MMKV 里没匹配到 dbKey——确认微信正登录着，再试一次")

    pp = bytes.fromhex(key)
    done, skipped = 0, 0
    dbs = wx.list_dbs(acc)
    meta_stems = {"contact", "session"}
    targets = [d for d in dbs
               if re.fullmatch(r"(contact|session|message_\d+)(-.*)?",
                               os.path.splitext(os.path.basename(d))[0])]
    if not targets:
        raise RuntimeError(f"{who} 下没找到消息库")

    for src in targets:
        stem = os.path.splitext(os.path.basename(src))[0]
        base = stem.rsplit("-", 1)[0] if "-" in stem else stem
        outdir = _outdir_for(stem)
        os.makedirs(outdir, exist_ok=True)
        dst = os.path.join(outdir, base + ".db")
        # contact/session 解过且还新鲜就跳过；message 库总是重解拿最新记录
        if base in meta_stems and os.path.exists(dst) \
                and os.path.getmtime(dst) > os.path.getmtime(src) - _META_FRESH:
            skipped += 1
            continue
        if progress:
            progress(f"解密 {os.path.basename(src)} …")
        sw = os.path.join(outdir, "self_wxid.txt")
        if not os.path.exists(sw):
            open(sw, "w", encoding="utf-8").write(who.rsplit("_", 1)[0]
                                                  if who.lower().startswith("wxid_") else who)
        tmp = dst + ".tmp"
        try:
            os.remove(tmp)
        except OSError:
            pass
        wx.decrypt_with_wal(pp, src, tmp)
        os.replace(tmp, dst)
        done += 1
    return f"{who}：新解密 {done} 个库，跳过 {skipped} 个（没变）"


if __name__ == "__main__":
    print(refresh(progress=print))
