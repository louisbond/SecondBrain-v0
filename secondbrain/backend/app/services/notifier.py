"""Windows 系统通知（toast）。

对齐 v0.5.0 §3.1 / §5.3：
「后台处理（抓取/转写/AI），完成或失败发 Windows toast」
「每周日 21:00 自动生成周报」

winotify 不可用时静默降级（不阻塞主流程），并把消息记入日志。
"""

from __future__ import annotations

import threading

from app.core.config import get_config

_lock = threading.Lock()
_available: bool | None = None


def _is_available() -> bool:
    global _available
    with _lock:
        if _available is not None:
            return _available
        try:
            import winotify  # type: ignore  # noqa: F401

            _available = True
        except Exception:
            _available = False
        return _available


def notify(
    title: str,
    message: str,
    *,
    sound: bool = True,
    timeout: int = 8,
) -> bool:
    """发一条系统通知。返回是否成功发出。"""
    cfg = get_config()
    if not cfg.notify.windows_toast:
        return False
    if not _is_available():
        print(f"[notify] {title}：{message}")
        return False

    try:
        from winotify import Notification, audio  # type: ignore

        # winotify 的签名是 (app_id, title, msg, icon, duration, launch) ——
        # **没有 timeout 参数**。之前多传一个 timeout，每次调用都 TypeError，
        # 被下面的 except 吞掉，于是「后台处理完发 toast」这个承诺从来没兑现过，
        # 只在控制台留一行「通知发送失败」。显示时长只能靠 duration 两档。
        toast = Notification(
            app_id="SecondBrain",
            title=title,
            msg=message,
            duration="long" if timeout > 5 else "short",
        )
        try:
            # audio 模块里也没有 `Looping`（只有 LoopingAlarm / LoopingCall 系列），
            # 静音/默认提示音才是稳的。
            toast.set_audio(audio.Default if sound else audio.Silent, loop=False)
        except Exception:  # noqa: BLE001
            pass
        toast.show()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[notify] 通知发送失败：{exc}")
        return False


# ---- 业务封装 ----------------------------------------------------------


def notify_batch_done(success: int, failed: int, duplicate: int) -> None:
    """批量会话结束汇总（§8.2）。"""
    notify(
        "SecondBrain · 本轮采集完成",
        f"成功 {success} 条、失败 {failed} 条（可手动补）、重复 {duplicate} 条",
    )


def notify_item_done(title: str, platform_label: str) -> None:
    notify("SecondBrain · 采集完成", f"{platform_label}｜{title}")


def notify_item_failed(title: str, reason: str) -> None:
    notify("SecondBrain · 采集失败", f"{title}\n{reason}")


def notify_asr_done(title: str) -> None:
    notify("SecondBrain · 转写完成", title)


def notify_review_ready(count: int) -> None:
    notify("SecondBrain · 有待核准条目", f"{count} 条等待你确认")


def notify_weekly_report(path: str) -> None:
    notify("SecondBrain · 周报已生成", path)


def notify_fallback(provider: str, reason: str) -> None:
    """AI 主链降级提示（§6.1）。"""
    notify(
        "SecondBrain · AI 已切换备链",
        f"{provider} 失败，已自动降级到 ollama。\n{reason}",
        sound=True,
        timeout=12,
    )
