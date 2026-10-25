# -*- coding: utf-8 -*-
"""读微信 4.x（Weixin）本地数据库：找解密 key → 解密 message/contact 库 → 抽聊天记录。

跟 OCR 旁挂是两条独立的路：OCR 只能看屏幕上滚出来的最近几条，这里读的是本地
db_storage 里的全部历史——用来蒸馏「我的性格和聊天方式」，喂给起草模型。

风险声明（自用工具，KICKOFF「只读自己设备的对话」范围内）：
- 读的是你自己登录账号的本地数据库，不碰网络、不碰别人的设备；
- key 靠扫微信进程内存 / 本地文件获得，需要微信正在运行；扫内存那段要管理员权限；
- 解密出来的明文库放在临时目录，用完即删，不落 OneDrive。

微信 4.x 的库是 SQLCipher 加密（实测 4.1.x）：AES-256-CBC，PBKDF2 派生 key，
参数不公开也不保证每个小版本一样，所以这里不硬编码单一参数——拿候选 key 对
message 库的第一页试解，解出 SQLite 头才算数（PARAM_SETS 里从最可能的开始试）。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import ctypes
from ctypes import wintypes  # noqa: F401  MEMORY_BASIC_INFORMATION 结构体要用

_HEX64 = re.compile(rb"[0-9a-f]{64}")
# 试解参数：(kdf_iter, hash 名称, reserve 字节数)。reserve = 每页尾部的 iv+hmac 区，
# SQLCipher 4 默认 80（iv16+sha512的64），老版本 48（iv16+sha1的20+padding）。
# 微信不公开自己的参数，这里从最可能的开始试，解出 SQLite 头才算数。
PARAM_SETS = [
    (256000, "sha512", 80),  # SQLCipher 4 默认，微信 4.x 大概率是这个
    (64000, "sha1", 48),     # 微信 3.x 老参数
    (64000, "sha512", 80),
    (256000, "sha1", 48),
    (1024, "sha1", 48),
]
_SQLITE_HEAD = b"SQLite format 3\x00"


def find_data_root() -> str | None:
    """微信 4.x 数据目录：文档目录下的 xwechat_files（“我的文档”可能被 OneDrive 重定向）。"""
    doc = os.path.join(os.environ.get("USERPROFILE", ""), "Documents")
    for doc in ([os.environ.get("USERPROFILE", "") + "/Documents"]
                + _known_documents()):
        root = os.path.join(doc, "xwechat_files")
        if os.path.isdir(root):
            return root
    return None


def _known_documents() -> list[str]:
    """注册表里「我的文档」的真实位置（Shell Folders），OneDrive 重定向就靠它。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            personal = str(winreg.QueryValueEx(k, "Personal")[0])
        return [personal]
    except Exception:
        return []


def latest_account(root: str) -> str | None:
    """最近登录过的账号目录（wxid_*，按 db_storage 的修改时间挑最新的）。"""
    best, best_t = None, -1.0
    for name in os.listdir(root):
        acc = os.path.join(root, name)
        db = os.path.join(acc, "db_storage")
        if not name.startswith("wxid") or not os.path.isdir(db):
            continue
        try:
            t = os.path.getmtime(db)
        except OSError:
            continue
        if t > best_t:
            best, best_t = acc, t
    return best


def db_path(account: str, name: str) -> str:
    return os.path.join(account, "db_storage", "message", name)


def _try_key(key: bytes, first_page: bytes, only: tuple | None = None) -> tuple[int, int, int] | None:
    """候选 key 对第一页试解：页头 16 字节 salt（明文），页尾 reserve 区开头是 iv，
    中间整段密文。解出来开头是 SQLite 头 → key 和布局参数都对了。
    返回 (kdf_iter, hash, reserve)。only 指定只试某一组参数（并行验证提速用）。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt = first_page[:16]
    ps = len(first_page)
    for kdf_iter, h, reserve in ([only] if only else PARAM_SETS):
        if ps - reserve - 16 < 32:
            continue
        iv = first_page[ps - reserve:ps - reserve + 16]
        blob = first_page[16:ps - reserve]
        dk = hashlib.pbkdf2_hmac(h, key, salt, kdf_iter, 32)
        dec = Cipher(algorithms.AES(dk), modes.CBC(iv)).decryptor()
        try:
            head = dec.update(blob[:48])
        except Exception:
            continue
        if head[:16] == _SQLITE_HEAD:
            return (kdf_iter, h, reserve)
    return None


def scan_key(account: str, db_file: str | None = None, progress=None) -> tuple[bytes, tuple[int, int, int]] | None:
    """找解密 key。两条路，从便宜到贵：
    1. 扫账号目录下所有小文件里的 64 位 hex 串（有些版本把 key 以 hex 落在本地配置里）；
    2. 扫 Weixin.exe 进程内存里的 64 位 hex 串（key 在内存里常以 hex'...' 的 PRAGMA 形式出现）。
    每个候选对 message 库第一页做一次 PBKDF2+AES 试解，试出来才算真 key。
    返回 (key, (kdf_iter, hash, reserve))；找不到返回 None。
    progress(done, total) 可选，给 UI 显示进度。"""
    db_file = db_file or db_path(account, "message_0.db")
    with open(db_file, "rb") as f:
        first_page = f.read(4096)

    def check(key: bytes):
        params = _try_key(key, first_page)
        return (key, params) if params else None

    # 1) 本地文件
    hexes, seen = [], set()
    for dirpath, _, files in os.walk(account):
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(p) > 60_000_000:
                    continue
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            for m in _HEX64.finditer(data):
                h = m.group()
                if h not in seen:
                    seen.add(h)
                    hexes.append(bytes.fromhex(h.decode()))
    for key in hexes:
        got = check(key)
        if got:
            return got

    # 2) 进程内存（需要管理员；微信没开就到不了这里）
    mem_hexes = _scan_weixin_memory(progress)
    for key in mem_hexes:
        got = check(key)
        if got:
            return got
    return None


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD)]


def _scan_weixin_memory(progress=None, stats=None) -> list[bytes]:
    """读所有 Weixin.exe 进程的可读内存，捞出 64 位 hex 串当 key 候选。
    stats 传个 dict 进来可以拿到诊断统计（进程数/区域数/读取字节/失败原因）。"""
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.ReadProcessMemory.restype = wintypes.BOOL
    # PROCESS_QUERY_INFORMATION(0x0400) | PROCESS_VM_READ(0x0010)：VirtualQueryEx 要前者，缺了全盘 err=5
    PROCESS_ACCESS = 0x0410
    MEM_COMMIT = 0x1000
    READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}  # RO / RW / WC / X-R / X-RW / X-WC
    out, seen = [], set()

    import subprocess
    try:
        pids = subprocess.run(["powershell", "-NoProfile", "-Command",
                               "(Get-Process Weixin).Id"], capture_output=True, text=True,
                              timeout=20).stdout.split()
    except Exception as e:
        if stats is not None:
            stats["pid_enum_fail"] = str(e)
        return []
    if stats is not None:
        stats["pids"] = pids
    for pid_s in pids:
        if not pid_s.strip().isdigit():
            continue
        pid = int(pid_s)
        h = k32.OpenProcess(PROCESS_ACCESS, False, pid)
        if not h:
            if stats is not None:
                stats[f"open_fail_{pid}"] = ctypes.get_last_error()
            continue
        try:
            addr, mbi, read_bytes, regions = 0, MEMORY_BASIC_INFORMATION(), 0, 0
            while addr < 0x7FFFFFFFFFFF:
                if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                    break
                base = mbi.BaseAddress or 0  # c_void_p 值为 0 时 ctypes 给 None
                if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                        and mbi.RegionSize and mbi.RegionSize < 64 * 1024 * 1024):
                    buf = (ctypes.c_char * mbi.RegionSize)()
                    got = ctypes.c_size_t()
                    if k32.ReadProcessMemory(h, ctypes.c_void_p(base),
                                             buf, mbi.RegionSize, ctypes.byref(got)):
                        data = buf.raw[:got.value]
                        read_bytes += got.value
                        regions += 1
                        for m in _HEX64.finditer(data):
                            hx = m.group()
                            if hx not in seen:
                                seen.add(hx)
                                out.append(bytes.fromhex(hx.decode()))
                addr = base + mbi.RegionSize
                if progress:
                    progress(addr, 0x7FFFFFFFFFFF)
            if stats is not None:
                stats[f"pid_{pid}"] = {"regions": regions, "bytes": read_bytes}
        finally:
            k32.CloseHandle(h)
    return out


def decrypt_db(src: str, found: tuple[bytes, tuple[int, int, int]], dst: str | None = None) -> str:
    """整库解密成明文 SQLite。WAL 不合并（少最新几条，蒸馏场景无所谓）。返回明文库路径。
    found = scan_key 的返回：(key, (kdf_iter, hash, reserve))。
    每页布局：第一页 [0:16]=salt，其余页 [0:16] 起就是密文；页尾 reserve 区开头 16 字节是 iv。
    明文页 = 解密结果 + 零填充到整页（SQLite 读的时候页尾 reserve 区本来就不放数据）。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key, (kdf_iter, h, reserve) = found
    page_size = 4096
    dst = dst or os.path.join(tempfile.mkdtemp(prefix="jev_wx_"), os.path.basename(src))
    with open(src, "rb") as f:
        raw = f.read()
    salt = raw[:16]
    dk = hashlib.pbkdf2_hmac(h, key, salt, kdf_iter, 32)
    pages = len(raw) // page_size
    with open(dst, "wb") as out:
        for i in range(pages):
            page = raw[i * page_size:(i + 1) * page_size]
            iv = page[page_size - reserve:page_size - reserve + 16]
            blob = page[16:page_size - reserve] if i == 0 else page[:page_size - reserve]
            dec = Cipher(algorithms.AES(dk), modes.CBC(iv)).decryptor()
            try:
                plain = dec.update(blob) + dec.finalize()
            except Exception as e:
                raise RuntimeError(f"第 {i} 页解密失败（key 或参数不对？）: {e}")
            if i == 0:
                plain = _SQLITE_HEAD + plain[16:]  # salt 占了头 16 字节，还回 SQLite 头
            out.write(plain[:page_size - reserve])
            out.write(b"\x00" * reserve)
    return dst
