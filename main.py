# -*- coding: utf-8 -*-
"""父进程：只管界面。两条采集链——OCR 子进程（app/worker.py）盯当前窗口；「读取最新聊天记录」
开着时另有库轮询监控（core/monitor.py）盯所有会话。冒出新的对方消息才调 engine → 悬浮窗给 3 条候选 →
人点「填入」；设置里开了「自动发送」才会自动填+回车（跨会话会先搜索切换）。静默期零调用。
上下文、结果、聊天记录都按会话名分开存，切会话不串味。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
两个模型（判断 Jev / 起草语言模型）的来源和 key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import multiprocessing
import queue
import threading
import traceback
from collections import deque

from app import settings, update, worker
from app.capture import find_chat_hwnd
from app.fill import fill, press_enter
from app.overlay import Overlay
from app.version import VERSION
from core.engine import analyze
from app.i18n import T

# {会话名: {history, result, rev, target, senders}}：每个会话各自的上下文、上次结果和版本号，互不串味
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
chats = {}
state = {"area": None, "busy": False, "rerun": [], "hwnd": None, "chat": ""}
results = queue.Queue()
update_result = queue.Queue()  # 独立小队列，别跟 results 的 (kind, r, title, revision) 形状搅在一起
mon_q = queue.Queue()  # 库轮询监控（core/monitor.py）发现的新消息，父进程内部队列
monitor = None  # core.monitor.Monitor 实例；read_latest 开关管着


def queue_rerun(title, msgs):
    """多会话排队：同会话只留最新一笔，先来先跑（旧版单槽覆盖会让排队的会话饿死）。"""
    state["rerun"] = [(t, m) for t, m in state["rerun"] if t != title]
    state["rerun"].append((title, msgs))


def chat_of(title):
    return chats.setdefault(title, {"history": deque(maxlen=60), "result": None, "rev": 0,
                                    "target": None, "senders": []})


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    chat = chat_of(title)
    if chat["target"] in chat["senders"]:
        return chat["target"]
    return chat["senders"][0] if chat["senders"] else None


def fill_reply(text, title=None):
    if state["hwnd"] is None:  # 子进程重开过，hwnd 可能换了，用最新的
        raise RuntimeError(T("未找到聊天窗口，请确认已经打开"))
    if state["area"] is None:
        raise RuntimeError(T("输入区域尚不可用，请确认聊天窗口可见（不要最小化）"))
    title = title or ov.current_chat()
    if settings.reply_target() and ov.at_prefix_enabled():
        target = target_of(title)
        if target:
            text = f"@{target} " + text  # 纯文本，微信不认成真正的 @，只是让群里看得出在跟谁说
    fill(state["hwnd"], state["area"], text)


def start_monitor():
    """库轮询监控：采集开着 + 「读取最新聊天记录」开着才启动。发现所有会话的新消息，
    当前 OCR 正在看的会话跳过（避免跟子进程双份），其余的排队分析。"""
    global monitor
    if monitor is None:
        from core.monitor import Monitor
        monitor = Monitor(new_cb=lambda *a: mon_q.put(a),
                          log_cb=lambda s: results.put(("log", s, "", 0)))
    monitor.start()


def stop_monitor():
    if monitor is not None:
        monitor.stop()


def on_monitor_new(name, who, text, sender):
    """监控发现的新消息（tick 里调）。喂进该会话的 history，对方说话就排队分析。"""
    chat = chat_of(name)
    chat["rev"] += 1  # 这个会话在跑的分析作废，跟 OCR 路径一个规矩
    chat["history"].append((who, text, sender))
    ov.log_message(who, text, sender, chat=name)
    if who == "her" and sender:  # 群里发言人列表，跟 OCR 路径一个规矩
        if sender in chat["senders"]:
            chat["senders"].remove(sender)
        chat["senders"].insert(0, sender)
    if who != "her":
        return
    maybe_refresh_learn(name)  # 第一次聊到的人顺手学风格（学过的秒退）
    if state["busy"]:
        queue_rerun(name, list(chat["history"]))
    else:
        start_analyze(name, list(chat["history"]))


def spawn_worker():
    """开一个采集子进程，它跟着 capture_on 走：置位=采集，清掉=暂停。"""
    p = multiprocessing.Process(target=worker.run,
                                args=(q, state["hwnd"], capture_on, debug_on, state.get("app")),
                                daemon=True)
    p.start()
    return p


def set_debug(on):
    """调试视图开关：开 → 开窗 + 置位（子进程这才开始送帧，一帧 2~3MB）；关 → 清掉 + 收窗。"""
    global dbg
    if not on:
        debug_on.clear()
        if dbg is not None:
            dbg.hide()
        return
    if dbg is None:
        from app.debugwin import DebugWindow

        dbg = DebugWindow(on_close=on_debug_closed)
    dbg.show()
    debug_on.set()


def on_debug_closed():
    """用户直接关了调试窗 = 把开关也关了，否则设置页显示开着但没窗。"""
    debug_on.clear()
    ov.set_debug_switch(False)
    settings.save(debug_view_on=False)


def on_language_changed():
    """主界面切换语言后，同步刷新已经打开过的调试窗。"""
    if dbg is not None:
        dbg.retranslate()


def on_toggle_capture(on):
    """标题栏开关。启动时没找到微信就没有子进程，这会儿再找一次，找到了才真开得起来。"""
    global child
    if not on:
        capture_on.clear()
        stop_monitor()
        return
    if child is None:
        try:
            state["hwnd"], found = find_chat_hwnd()
            state["app"] = found.key
        except RuntimeError:
            ov.set_capture(False, "未找到聊天窗口，打开后再开启采集")
            return
        child = spawn_worker()
    capture_on.set()
    if settings.read_latest():  # 库轮询监控多会话；开关关了只盯当前窗口
        start_monitor()


def analyze_bg(msgs, title, revision, reply_to=None):
    """后台线程只跑网络调用，结果丢队列；UI 只在主线程的 tick 里动（Qt 不能跨线程碰）。"""
    try:
        # 只跟绑定了关系的人聊天：开关开着而这个会话没绑关系，就只判断不起草，
        # 空候选回到 tick 后既不弹卡也不会触发自动发送
        allow_draft = not (settings.bound_only() and title not in settings.contact_rels())
        if not allow_draft:
            results.put(("log", f"[只判断]「{title}」没绑关系：不起草不发送。"
                                f"在设置页给TA绑个关系，或关掉「只跟绑定的人聊天」", title, 0))
        results.put(("ok", analyze(msgs, settings.contact_rel(title), context=settings.context(),
                                   model=settings.draft_model() or None,
                                   provider=settings.draft_provider(),
                                   base_url=settings.draft_base_url() or None,
                                   reply_to=reply_to, style=settings.style(),
                                   thinking=settings.thinking(),
                                   jev_provider=settings.jev_provider(),
                                   jev_model=settings.jev_model() or None,
                                   contact=title, draft=allow_draft),
                     title, revision))
    except Exception as e:
        results.put(("err", f"{T('分析失败: ')}{e}", title, revision))


def _draft_callable():
    """给 contact_style.learn 用的起草函数：(system, user) -> 文本。用当前设置里的起草来源。"""
    from core import llm, providers
    spec = providers.DRAFT_PROVIDERS[settings.draft_provider()]

    def call(system: str, user: str) -> str:
        return llm.chat(spec.protocol, settings.draft_base_url() or spec.base or None,
                        settings.llm_key(), settings.draft_model() or spec.default,
                        system, [user], max_tokens=512)
    return call


_learning = set()  # 正在刷新/学习中的会话名，防同一个联系人重入


def refresh_learn_bg(title, force=False):
    """后台链：开关开 → 先从微信库解密出最新记录（core/wx_refresh）；
    然后没学过/学太旧（或手动强制）→ 学这个人的风格。
    全程本机操作 + 一次模型调用。失败只 log，不影响采集和分析。"""
    from core import contact_style
    try:
        if settings.read_latest():
            from core import wx_refresh
            results.put(("log", "正在从微信读取最新聊天记录…", title, 0))
            results.put(("log", wx_refresh.refresh(), title, 0))
        if force or contact_style.is_stale(title):
            style = contact_style.learn(title, _draft_callable())
            results.put(("log", f"[风格] 已学习「{title}」：{(style.splitlines() or [''])[0][:40]}",
                         title, 0))
        elif force is False:
            pass  # 学过且没过期，什么都不用做
    except Exception as e:
        results.put(("log", f"[风格] 「{title}」跳过：{e}", title, 0))
    finally:
        _learning.discard(title)


def maybe_refresh_learn(title, force=False):
    """切换会话 / 手动点「学TA风格」的入口。群聊不学（风格是对单个联系人说的）。"""
    if not title or title.endswith(")") or title in _learning:
        return
    _learning.add(title)
    threading.Thread(target=refresh_learn_bg, args=(title, force), daemon=True).start()


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def start_analyze(title, msgs):
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        return
    if not settings.has_llm_key():
        ov.set_status(lambda name=settings.draft_provider_name():
                      f"{T('起草来源 ')}{name}{T(' 没填密钥，去设置里补上')}", "warning")
        return
    # 风格学习不在分析前抢跑：切会话时（chat 消息）已触发 refresh+learn 链，
    # analyze 里 engine 会优先用学过的 per-contact 风格，没学过自动退回全局 style
    state["busy"] = True
    ov.set_busy(True)
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg, args=(msgs, title, chat_of(title)["rev"], reply_to),
                     daemon=True).start()


def on_target_change(title, name):
    """用户挑了回复对象：记下来，这个会话里有对方的话就照新对象重跑一次。"""
    chat = chat_of(title)
    chat["target"] = name
    msgs = list(chat["history"])
    if not any(m[0] == "her" for m in msgs):
        return
    if state["busy"]:
        queue_rerun(title, msgs)
        ov.set_busy(True)
    else:
        start_analyze(title, msgs)


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            return
        kind = msg[0]
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 微信切了会话，界面跟过去（用户正浏览别的会话时也跟，微信是准的）
            state["chat"] = msg[1]
            ov.set_chat(msg[1])
            maybe_refresh_learn(msg[1])  # 切了对象就按需刷新记录+更新此人的说话风格
            continue
        if kind == "debug":  # 调试视图的一帧；窗口不在就直接丢掉
            if dbg is not None:
                dbg.show_packet(msg[1])
            continue
        if kind == "status":  # 单帧识别失败/报错，提示一下就好，别把正在跑的分析和已知坐标清掉
            ov.set_status(msg[1], "warning")
            ov.log(msg[1])
            continue
        if kind == "paused":  # 子进程确认已暂停
            ov.set_capture(False)
            continue
        if kind == "resumed":  # 子进程重新开始采集
            ov.set_capture(True)
            continue
        if kind == "dead":  # 采集彻底停了（微信关了之类），这才是真的要清状态
            state["area"] = None
            for c in chats.values():  # 在跑的分析作废，回来的结果不再往界面上贴
                c["rev"] += 1
            state["rerun"] = []
            ov.invalidate_replies()
            ov.set_busy(False)
            ov.set_capture(False, msg[1])
            ov.log(msg[1])
            if child is not None:  # 子进程已经不干活了，收掉引用，下次打开开关重开一个
                child.terminate()
                child.join()
                child = None
            continue
        _, title, new, area = msg
        state["area"] = area
        chat = chat_of(title)
        chat["rev"] += 1  # 这个会话有新消息了，它在跑的分析作废
        if title == ov.current_chat():  # 看的是别的会话就别把人家的候选划掉
            ov.invalidate_replies()
        for who, name, text in new:
            chat["history"].append((who, text, name))
            ov.log_message(who, text, name, chat=title)
            if who == "her" and name:  # 群里发过言的人，去重后最近的排最前
                if name in chat["senders"]:
                    chat["senders"].remove(name)
                chat["senders"].insert(0, name)
        ov.set_targets(title, chat["senders"], target_of(title))  # 显不显示这一行由悬浮窗按开关决定
        if new[-1][0] == "her":  # 只有对方最新说话才值得分析
            msgs = list(chat["history"])
            if state["busy"]:
                queue_rerun(title, msgs)
                ov.set_busy(True)
            else:
                start_analyze(title, msgs)
        else:
            ov.set_busy(False)
            ov.set_status("你已回复，等待对方的新消息")


def tick():
    try:
        drain()
        while not mon_q.empty():  # 库轮询发现的多会话新消息
            name, who, text, sender = mon_q.get()
            if name == ov.current_chat():
                continue  # 这个会话 OCR 子进程正在看，走正常 msgs 路径，不双份
            on_monitor_new(name, who, text, sender)
        while not update_result.empty():
            latest, url = update_result.get()
            ov.set_update(latest, url)
        while not results.empty():
            kind, r, title, revision = results.get()
            if kind == "log":  # 刷新/学习/监控链的进度消息，跟分析结果无关，不碰 busy/revision
                ov.log(r)
                continue
            state["busy"] = False
            if revision != chat_of(title)["rev"]:  # 这个会话后来又说话了，这份结果过期了
                ov.set_busy(False)
            elif kind == "ok":
                chat_of(title)["result"] = r  # 先存着；正看着这个会话才立刻贴上去
                if title == ov.current_chat():
                    ov.show(r)
                else:
                    ov.set_busy(False)
                if settings.auto_send() and r.get("best_reply"):  # 自动发送：填入+回车，不经过人眼
                    try:
                        if title != ov.current_chat():  # 监控发现的别的会话：先切过去
                            from app import fill as fill_mod
                            fill_mod.switch_to(state["hwnd"], title)
                        fill_reply(r["best_reply"], title=title)
                        press_enter()
                        ov.log(f"[自动发送] 已发给「{title}」：{r['best_reply'][:30]}")
                    except Exception as e:
                        ov.log(f"[自动发送] 失败：{e}")
            else:
                ov.set_busy(False)
                ov.set_status("生成失败，请检查网络和服务设置；新消息到来后会重试。", "error")
                ov.log(r)
            if state["rerun"]:  # 这条处理完（或丢掉），接着跑排队的下一个会话
                t, msgs = state["rerun"].pop(0)
                start_analyze(t, msgs)
    except Exception:
        traceback.print_exc()  # 一帧出错不退出
    ov.after(50, tick)


if __name__ == "__main__":  # Windows 的 spawn 会让子进程重新执行本文件，没这行就无限套娃开进程
    multiprocessing.freeze_support()  # 打包成 exe 后 spawn 出来的子进程会重跑一遍 exe，没这行就无限弹界面
    ctypes.windll.user32.SetProcessDPIAware()
    q = multiprocessing.Queue()
    capture_on = multiprocessing.Event()  # 父子进程共用的开关，置位=采集
    debug_on = multiprocessing.Event()  # 同上，置位=子进程往队列里送整帧给调试窗
    child = dbg = None
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_language_changed=on_language_changed,
                 result_of=lambda t: chats.get(t, {}).get("result"))
    # 「学TA风格」按钮：手动强制重学当前联系人（开关开时会先从微信库拉最新记录）
    ov.learnBtn.clicked.connect(
        lambda: maybe_refresh_learn(ov.current_chat(), force=True))
    try:
        state["hwnd"], found = find_chat_hwnd()
        state["app"] = found.key
    except RuntimeError:
        ov.set_capture(False, "未找到聊天窗口，打开后再开启采集")
    else:
        capture_on.set()
        child = spawn_worker()
        if settings.read_latest():  # 多会话监控跟着采集一起开
            start_monitor()
    if settings.debug_view():  # 上次开着就直接开回来
        set_debug(True)
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        ov.after(0, ov.open_settings)
    if settings.check_update() and update.parse_version(VERSION):  # 开发版没有版本号，不查也不烦源码用户
        threading.Thread(target=check_update_bg, daemon=True).start()
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        stop_monitor()
        if child is not None:
            child.terminate()
