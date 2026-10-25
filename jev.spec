# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包定义，CI（.github/workflows/release.yml）和 build.bat 共用这一份。
onedir 不是 onefile：PySide6 + onnxruntime 打出来 ~150MB，onefile 每次启动都要解压一遍，慢且占临时盘。
只在 Windows 上跑，下面的 collect_all 也只认 Windows 上装好的那几个包。"""
from PyInstaller.utils.hooks import collect_all
import os
import shutil

NAME = "jev-chat-windows"

hiddenimports = [
    # spawn 出来的采集子进程按名字 import app.worker，再顺着它拉 capture/ocr；
    # 父进程这边 engine 也是运行时才走到，一并钉死，别指望静态分析都能扫出来
    "app.worker", "app.capture", "app.ocr", "app.fill", "app.overlay", "app.settings",
    "app.version", "app.update", "app.debugwin",  # debugwin 是开了调试视图才 import 的
    "app.i18n", "app.chatapps", "app.ocr_windows",
    "core.engine", "core.draft", "core.jev_client", "core.questions", "core.providers",
    "core.llm", "core.contact_style", "core.wx_refresh", "core.wechat_db", "core.monitor",
    "wx_extract",  # wx_refresh 运行时 sys.path 加 tools/ 再 import，静态分析扫不到
    "win32api",  # qfluentwidgets 运行时 import，PyInstaller 没扫到
]
# 打包会重建 dist\jev-chat-windows\，exe 旁边的运行时数据（config.json 设置+已学风格、
# tools\decrypted* 解密库）会被清掉。备份到 dist 外面（build\_keep），打完恢复。
_DIST = os.path.abspath(os.path.join("dist", "jev-chat-windows"))
_KEEP_DIR = os.path.abspath(os.path.join("build", "_keep"))
if os.path.isdir(_KEEP_DIR):
    shutil.rmtree(_KEEP_DIR)
os.makedirs(_KEEP_DIR, exist_ok=True)
_keep = []  # (相对路径, 备份出来的临时绝对路径)
_keep_rels = ["config.json"]
_dist_tools = os.path.join(_DIST, "tools")
if os.path.isdir(_dist_tools):
    for d in os.listdir(_dist_tools):
        if d.startswith("decrypted"):
            _keep_rels.append(os.path.join("tools", d))
for rel in _keep_rels:
    src = os.path.join(_DIST, rel)
    if os.path.exists(src):
        tmp = os.path.join(_KEEP_DIR, rel.replace(os.sep, "__"))
        if os.path.isdir(src):
            shutil.copytree(src, tmp)
        else:
            shutil.copy2(src, tmp)
        _keep.append((rel, tmp))
datas, binaries = [], []
datas += [("docs/wechat-mp.png", "docs")]  # 设置页底部的公众号长条横幅
datas += [("tools/wx_extract.py", "tools")]  # 微信库解密（wx_refresh 从 _MEIPASS/tools 加载）
for pkg in (
    "rapidocr_onnxruntime",  # .onnx 模型 + config.yaml 是包数据，不收就是启动即炸
    "onnxruntime",           # capi 下面那堆 DLL
    "qfluentwidgets",        # qss / 图标资源
    "windows_capture",       # Rust 编译的 .pyd
    # 四个模型 SDK：core/llm.py 和 jev_client 里是**函数内 import**，静态分析扫不到，必须显式收
    "openai",
    "typesafe_sdk",
    "anthropic",
    "google.genai",
    "certifi",               # httpx 的 CA 证书包；certifi 的官方 hook 通常收得到，这里写明白省得漏
    "cryptography",          # wx_extract 解密微信库（AES-256-GCM）
    "zstandard",             # contact_style 解 message_content 的 zstd 压缩
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

excludes = [
    # 确认没人用：rapidocr 只 import 了 cv2 / PIL / yaml / pyclipper / shapely（PIL 千万别排，读图要它）
    "tkinter", "matplotlib", "scipy", "pandas",
] + ["PySide6." + m for m in (
    # 留着 QtCore / QtGui / QtWidgets / QtSvg / QtSvgWidgets / QtXml —— import qfluentwidgets 实测就这六个
    "QtWebEngineCore", "QtWebEngineWidgets", "QtWebEngineQuick", "QtWebChannel",
    "QtMultimedia", "QtMultimediaWidgets", "QtCharts", "QtDataVisualization",
    "QtQuick", "QtQuick3D", "QtQuickControls2", "QtQuickWidgets", "QtQuickTest", "QtQml",
    "QtPdf", "QtPdfWidgets", "QtBluetooth", "QtNfc", "QtSensors", "QtSerialPort",
    "QtTest", "QtDesigner", "QtHelp", "QtRemoteObjects", "QtScxml", "QtStateMachine",
    "QtTextToSpeech", "QtPositioning", "QtLocation", "QtSql",
    "Qt3DCore", "Qt3DRender", "Qt3DInput", "Qt3DLogic", "Qt3DAnimation", "Qt3DExtras",
)]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # runner 上本来就没 upx，而且压 Qt / onnxruntime 的 DLL 是出了名的能压坏
    console=False,  # 不要黑框；print 也就跟着没了，状态界面上都有，聊天内容本来就不许落日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="docs/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=NAME,
)

# 恢复打包前备份的运行时数据（config.json / tools\decrypted*）
for rel, tmp in _keep:
    dst = os.path.join(_DIST, rel)
    if os.path.isdir(tmp):
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.move(tmp, dst)
    else:
        os.makedirs(os.path.dirname(dst) or _DIST, exist_ok=True)
        shutil.move(tmp, dst)
    print(f"[spec] 已恢复运行时数据: {rel}")
shutil.rmtree(_KEEP_DIR, ignore_errors=True)
