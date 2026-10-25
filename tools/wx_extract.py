# -*- coding: utf-8 -*-
"""微信 4.x 多账号通用提取器（配合 viewer.exe / viewer.py 使用）
- 自动发现 xwechat_files 下所有账号目录（多账号均支持）
- 对指定账号目录用 dbKey 解密全部 message/contact/session 库（含 WAL 合并）
- 输出到 out_root:  decrypted/（主库）  decrypted_<后缀>/（备用库，如 -副号）
CLI:
  python wx_extract.py                    列出发现的账号目录
  python wx_extract.py <账号目录> <dbKey>  解密指定账号
"""
import os, sys, glob, re, struct, time, hashlib, hmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE = 4096
IV_END = PAGE - 64      # 4032
CT_END = PAGE - 80      # 4016
MAGIC = b'SQLite format 3\x00'


def find_accounts():
    """在常见位置找 xwechat_files，返回 [账号目录路径, ...]（每个目录含 db_storage）"""
    up = os.path.expanduser('~')
    cands = [os.path.join(up, 'Documents'),
             os.path.join(up, 'OneDrive', '文档'),
             os.path.join(up, 'OneDrive', 'Documents'),
             os.path.join(up, '文档'),
             up]
    roots = []
    for c in cands:
        if not os.path.isdir(c):
            continue
        p = os.path.join(c, 'xwechat_files')
        if os.path.isdir(p):
            roots.append(p)
            continue
        try:
            for d in os.listdir(c):
                q = os.path.join(c, d, 'xwechat_files')
                if os.path.isdir(q):
                    roots.append(q)
        except OSError:
            pass
    out = []
    for r in roots:
        try:
            for d in os.listdir(r):
                if os.path.isdir(os.path.join(r, d, 'db_storage')):
                    out.append(os.path.join(r, d))
        except OSError:
            pass
    return sorted(set(out))


def list_dbs(acc_dir):
    """列出账号目录下可解密的库（message/contact/session，排除媒体与临时文件）"""
    store = os.path.join(acc_dir, 'db_storage')
    out = []
    if not os.path.isdir(store):
        return out
    for sub in ('message', 'contact', 'session'):
        for p in sorted(glob.glob(os.path.join(store, sub, '*.db'))):
            name = os.path.basename(p)
            stem = os.path.splitext(name)[0]
            low = stem.lower()
            if low.startswith('media') or low.startswith('media_'):
                continue
            if not low.startswith(('message_', 'contact', 'session')):
                continue
            out.append(p)
    return out


def keys_for(salt, pp):
    raw = hashlib.pbkdf2_hmac('sha512', pp, salt, 256000, 32)
    mk = hashlib.pbkdf2_hmac('sha512', raw, bytes(x ^ 0x3A for x in salt), 2, 32)
    return raw, mk


def dec_page(pg, pgno, raw, mk):
    ct_off = 16 if pgno == 1 else 0
    if hmac.new(mk, pg[ct_off:IV_END] + struct.pack('<I', pgno), hashlib.sha512).digest() != pg[IV_END:]:
        return None
    d = Cipher(algorithms.AES256(raw), modes.CBC(pg[IV_END - 16:IV_END])).decryptor()
    pt = d.update(pg[ct_off:CT_END]) + d.finalize()
    return (MAGIC if pgno == 1 else b'') + pt + bytes(PAGE - ct_off - len(pt))


def decrypt_with_wal(pp, src, dst):
    """主库+WAL 帧重放解密。页1 HMAC 失败抛 ValueError（= 密钥错误）。"""
    data = open(src, 'rb').read()
    if len(data) < PAGE:
        raise ValueError(f'文件过小: {src}')
    salt = data[:16]
    raw, mk = keys_for(salt, pp)
    pages = {}
    for i in range(len(data) // PAGE):
        p = dec_page(data[i * PAGE:(i + 1) * PAGE], i + 1, raw, mk)
        if p is not None:
            pages[i + 1] = p
    if 1 not in pages:
        raise ValueError(f'页1校验失败，密钥错误或文件损坏: {src}')
    npages_main = len(data) // PAGE
    nwal = 0
    wal_path = src + '-wal'
    if os.path.exists(wal_path):
        w = open(wal_path, 'rb').read()
        if len(w) >= 32:
            magic, fmt, psz, ckpt, s1, s2 = struct.unpack('>6I', w[:24])
            if magic in (0x377f0682, 0x377f0683) and psz == PAGE:
                pending = []
                dbsize = npages_main
                off = 32
                while off + 24 + PAGE <= len(w):
                    pgno, commit, fs1, fs2 = struct.unpack('>4I', w[off:off + 16])
                    if (fs1, fs2) != (s1, s2):
                        break
                    pg = w[off + 24:off + 24 + PAGE]
                    p = dec_page(pg, pgno, raw, mk)
                    if p is not None:
                        pending.append((pgno, p))
                        if commit:
                            for n2, pp2 in pending:
                                pages[n2] = pp2
                                nwal += 1
                            pending = []
                            dbsize = commit
                    off += 24 + PAGE
                total = dbsize
            else:
                total = npages_main
        else:
            total = npages_main
    else:
        total = npages_main
    out = bytearray()
    for i in range(1, total + 1):
        out += pages.get(i, bytes(PAGE))
    open(dst, 'wb').write(bytes(out))
    return npages_main, nwal, total


def wxid_of(acc_dir):
    base_name = os.path.basename(os.path.normpath(acc_dir))
    return base_name.rsplit('_', 1)[0] if base_name.lower().startswith('wxid_') else base_name


def find_dbkey_candidates(acc_dir):
    """从 %APPDATA%\\Tencent\\<wxid>*mmkv* 读 MMKV 文件，提取 dbKey 候选。
    返回 (优先候选[dbkey标签附近], 其他64hex候选)。密钥由 a2auth 接口写入 MMKV，明文 hex。"""
    wxid = wxid_of(acc_dir)
    appdata = os.environ.get('APPDATA', '')
    tdir = os.path.join(appdata, 'Tencent')
    dirs = []
    if os.path.isdir(tdir):
        for d in os.listdir(tdir):
            if wxid.lower() in d.lower() and 'mmkv' in d.lower():
                dirs.append(os.path.join(tdir, d))
    primary, generic = [], []
    seen = set()
    for dd in dirs:
        for p in sorted(glob.glob(os.path.join(dd, '*'))):
            if not os.path.isfile(p):
                continue
            try:
                data = open(p, 'rb').read()
            except OSError:
                continue
            for m in re.finditer(rb'dbkey', data, re.I):
                seg = data[m.end():m.end() + 200]
                hm = re.search(rb'[0-9a-fA-F]{64}', seg)
                if hm:
                    v = hm.group().decode()
                    if v not in seen:
                        seen.add(v)
                        primary.append(v)
            for hm in re.finditer(rb'[0-9a-fA-F]{64}', data):
                v = hm.group().decode()
                if v not in seen:
                    seen.add(v)
                    generic.append(v)
    return primary, generic


def validate_pp(key_hex, acc_dir):
    """用账号下任一库的 page1 HMAC 验证 dbKey 是否正确"""
    pp = bytes.fromhex(key_hex)
    for src in list_dbs(acc_dir)[:3]:
        try:
            pg = open(src, 'rb').read(PAGE)
        except OSError:
            continue
        if len(pg) < PAGE:
            continue
        salt = pg[:16]
        raw, mk = keys_for(salt, pp)
        if hmac.new(mk, pg[16:4032] + struct.pack('<I', 1), hashlib.sha512).digest() == pg[4032:4096]:
            return True
    return False


def auto_key(acc_dir):
    """自动获取 dbKey：MMKV 候选 -> HMAC 验证。成功返回 64hex，失败返回 None"""
    primary, generic = find_dbkey_candidates(acc_dir)
    cands = list(dict.fromkeys(primary + generic))[:64]
    for c in cands:
        try:
            if validate_pp(c, acc_dir):
                return c.lower()
        except Exception:
            pass
    return None


def extract_account(acc_dir, key_hex, out_root, progress=None):
    """解密一个账号目录的全部库到 out_root/decrypted[_后缀]/。返回 [(源名, 主页数, WAL帧数, 总页数)]"""
    key_hex = key_hex.strip().lower()
    if len(key_hex) != 64:
        raise ValueError('dbKey 必须是 64 位十六进制')
    pp = bytes.fromhex(key_hex)
    dbs = list_dbs(acc_dir)
    if not dbs:
        raise ValueError('该目录下没有可解密的消息库')
    done = []
    for idx, src in enumerate(dbs):
        name = os.path.basename(src)
        stem = os.path.splitext(name)[0]
        if '-' in stem:
            base, suffix = stem.rsplit('-', 1)
            outdir = os.path.join(out_root, 'decrypted_' + suffix)
        else:
            base, outdir = stem, os.path.join(out_root, 'decrypted')
        os.makedirs(outdir, exist_ok=True)
        sw = os.path.join(outdir, 'self_wxid.txt')
        if not os.path.exists(sw):
            base_name = os.path.basename(os.path.normpath(acc_dir))
            wxid = base_name.rsplit('_', 1)[0] if base_name.lower().startswith('wxid_') else base_name
            open(sw, 'w').write(wxid)
        dst = os.path.join(outdir, base + '.db')
        if progress:
            progress(idx, len(dbs), f'({idx + 1}/{len(dbs)}) {name} …')
        try:
            os.remove(dst + '.tmp')
        except OSError:
            pass
        m, w, t = decrypt_with_wal(pp, src, dst + '.tmp')
        for _ in range(5):
            try:
                os.replace(dst + '.tmp', dst)
                break
            except PermissionError:
                try:
                    os.remove(dst)
                except OSError:
                    pass
                time.sleep(1)
        else:
            try:
                os.remove(dst + '.tmp')
            except OSError:
                pass
            raise ValueError(f'目标被占用无法覆盖（请重启软件后再导入）: {dst}')
        done.append((name, m, w, t))
        if progress:
            progress(idx + 1, len(dbs), f'({idx + 1}/{len(dbs)}) {name}: 主库{m}页+WAL{w}帧={t}页')
    return done


if __name__ == '__main__':
    if len(sys.argv) == 1:
        accs = find_accounts()
        print(f'发现 {len(accs)} 个账号目录:')
        for a in accs:
            print(f'  {a}   ({len(list_dbs(a))} 个可解密库)')
        print('\n用法: python wx_extract.py <账号目录> <dbKey>')
    elif len(sys.argv) == 3:
        res = extract_account(sys.argv[1], sys.argv[2],
                              os.path.dirname(os.path.abspath(__file__)),
                              progress=lambda c, t, m: print(m))
        print('完成', len(res), '个库')
    else:
        print(__doc__)
