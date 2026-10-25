"""Optional, best-effort bridge for one Windows WeChat group.

The app works without this module or wxautox4. Default mode reads readiness only;
it never forwards patient messages or sends to WeChat. Actual send mode requires
both --send and the server's live_enabled switch. See README before using it.
"""
from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid


class BridgeError(RuntimeError):
    """An error that requires deliberate operator recovery, never auto-retry."""


MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ImageTransferError(ValueError):
    """A recoverable local image problem, reported without private paths."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def is_link_or_reparse(path: Path, info=None) -> bool:
    info = info or path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


@contextmanager
def private_image_directory():
    """A per-message temporary tree, removed only if its root is still ours.

    Use explicit cleanup rather than an unconditional finalizer so a replaced
    root is never recursively removed. Python 3.10+ rmtree does not descend
    through Windows directory junctions or directory symlinks inside the tree.
    """
    parent = Path(tempfile.gettempdir()).resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix="family-health-image-", dir=parent)).resolve(strict=True)
    created = root.stat()
    try:
        yield root
    finally:
        try:
            current = root.lstat()
        except FileNotFoundError:
            current = None
        if current is not None:
            if (is_link_or_reparse(root, current) or not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino)
                    or root.resolve(strict=True).parent != parent):
                raise ImageTransferError("unsafe_path")
            try:
                shutil.rmtree(root)
            except OSError:
                raise ImageTransferError("download_failed") from None


def read_downloaded_image(returned, root: Path) -> bytes:
    """Read only a regular, bounded file under this message's private tree."""
    if not isinstance(returned, (str, os.PathLike)):
        # The author returns WxResponse on download failure, not a path.
        raise ImageTransferError("download_failed")
    try:
        path = Path(returned)
        if not path.is_absolute():
            path = root / path
        path = Path(os.path.abspath(path))
        relative = path.relative_to(root)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        if resolved == root:
            raise ImageTransferError("unsafe_path")
        current = root
        for part in relative.parts:
            current = current / part
            if is_link_or_reparse(current):
                raise ImageTransferError("unsafe_path")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ImageTransferError("unsafe_path")
        if info.st_size > MAX_IMAGE_BYTES:
            raise ImageTransferError("too_large")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ImageTransferError("unsafe_path")
            data = stream.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageTransferError("too_large")
        if not data:
            raise ImageTransferError("download_failed")
        return data
    except ImageTransferError:
        raise
    except (ValueError, RuntimeError):
        raise ImageTransferError("unsafe_path") from None
    except OSError:
        raise ImageTransferError("download_failed") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BridgeError("本地服务发生重定向，已停止；不会转发访问令牌。")


class ApiClient:
    def __init__(self, base_url: str, token: str):
        parsed = urllib.parse.urlsplit(base_url)
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            local = False
        # Require a literal loopback address: no DNS aliases or remote endpoints.
        if (not local or parsed.scheme != "http" or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise BridgeError("--server 只允许本机 HTTP 地址，例如 http://127.0.0.1:8765。")
        if not token or len(token) < 16 or "\n" in token or "\r" in token:
            raise BridgeError("桥接令牌缺失或无效。先启动主程序，再检查 token 文件。")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json", "Accept": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with self.opener.open(req, timeout=45) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise BridgeError("本地服务返回内容过大，桥接已停止。")
            result = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Do not print body, URL credentials, message contents, or tokens.
            raise BridgeError(f"本地服务返回 HTTP {exc.code}；本次操作不会自动重试。") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BridgeError("无法确认本地服务响应；已停止，不自动重试消息。") from None
        except (ValueError, UnicodeError):
            raise BridgeError("本地服务返回的 JSON 无效，已停止。") from None
        if not isinstance(result, dict):
            raise BridgeError("本地服务返回结构不匹配。")
        return result

    def heartbeat(self, status: str, detail: str) -> None:
        self.request("/api/bridge/heartbeat", {"status": status, "detail": detail})


@dataclass(frozen=True)
class GroupConfig:
    group_name: str
    patient_sender: str
    caregiver_sender: str
    live_enabled: bool

    @classmethod
    def from_dict(cls, raw: dict) -> "GroupConfig":
        values = []
        for key in ("group_name", "patient_sender", "caregiver_sender"):
            value = raw.get(key)
            if (not isinstance(value, str) or not value or value != value.strip()
                    or len(value) > 200 or any(c in value for c in "\r\n\x00")):
                raise BridgeError("请在网页设置中填写唯一群名、父亲群内显示名和家属群内显示名。")
            values.append(value)
        if values[1] == values[2]:
            raise BridgeError("父亲和家属的群内显示名必须不同。")
        return cls(*values, raw.get("live_enabled") is True)

    def identities(self) -> tuple[str, str, str]:
        return self.group_name, self.patient_sender, self.caregiver_sender


def field(message: Any, name: str, default=None):
    return message.get(name, default) if isinstance(message, dict) else getattr(message, name, default)


def digest(parts: list[str]) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


class FixedWindow:
    """wxautox4 documented surface; never switch conversations after seeding."""

    def __init__(self, wx: Any, group_name: str):
        self.wx = wx
        self.group_name = group_name

    def open_once(self) -> None:
        self.wx.ChatWith(who=self.group_name, exact=True)
        self.verify()

    def verify(self) -> None:
        info = self.wx.ChatInfo()
        if (not isinstance(info, dict) or info.get("chat_type") != "group"
                or info.get("chat_name") != self.group_name):
            raise BridgeError("当前微信窗口已离开指定群，桥接停止。确认群后手动重启；不自动切回。")
        online = getattr(self.wx, "IsOnline", None)
        if callable(online) and online() is not True:
            raise BridgeError("无法确认微信在线，桥接停止。请人工检查登录状态。")

    def snapshot(self) -> list:
        self.verify()
        messages = self.wx.GetAllMessage()
        self.verify()
        if not isinstance(messages, list):
            raise BridgeError("获取群消息失败或返回格式改变，桥接停止。")
        return messages

    def send(self, text: str) -> None:
        self.verify()
        # No 'who': sending must not switch to an unverified conversation.
        result = self.wx.SendMsg(msg=text, clear=True)
        self.verify()
        # The author's WxResponse contract uses bool(result) for success.
        if result is None or not bool(result):
            raise BridgeError("微信发送未确认成功；可能已发送，请人工核对，不自动重试。")


class SessionTracker:
    """Per-session UI-ID de-duplication, not a persistent WeChat message identity.

    UI IDs can change after rerendering; the author's hash can collide. Hash is
    diagnostic only. Never use message text or hash to suppress a real repeated
    measurement. Replaced viewports and reused IDs stop the session.
    """

    def __init__(self, max_seen: int = 10000):
        self.session_id = uuid.uuid4().hex
        self.seen: OrderedDict[str, str] = OrderedDict()
        self.hashes: OrderedDict[str, str] = OrderedDict()
        self.previous: set[str] = set()
        self.max_seen = max_seen

    def observe(self, messages: list, *, seed: bool = False) -> list[tuple[str, Any]]:
        current: dict[str, tuple[str, Any]] = {}
        for message in messages:
            # System time labels may have no ID and are never processed.
            if field(message, "attr") not in ("friend", "self"):
                continue
            ui_id = field(message, "id")
            if not isinstance(ui_id, str) or not ui_id or len(ui_id) > 1000:
                raise BridgeError("消息缺少可用 UI ID；无法在当前会话去重，桥接停止。")
            fingerprint = digest([str(field(message, k, "")) for k in ("attr", "type", "sender", "content")])
            if ui_id in current:
                raise BridgeError("当前消息窗口包含重复 UI ID，无法确认消息身份。")
            if ui_id in self.seen and self.seen[ui_id] != fingerprint:
                raise BridgeError("消息 UI ID 被重用或内容改变；请人工核对后重启桥接。")
            current[ui_id] = (fingerprint, message)
        current_ids = set(current)
        if not seed and self.previous and current_ids and not self.previous.intersection(current_ids):
            raise BridgeError("消息窗口整体重建或滚动过远，已停止以避免重放历史。")
        fresh = []
        for ui_id, (fingerprint, message) in current.items():
            if not seed and ui_id not in self.seen:
                fresh.append(("wx-session:" + self.session_id + ":" + digest([ui_id]), message))
            self.seen[ui_id] = fingerprint
            self.seen.move_to_end(ui_id)
            stable_hash = field(message, "hash")
            if isinstance(stable_hash, str) and stable_hash:
                self.hashes[ui_id] = digest([stable_hash])
                self.hashes.move_to_end(ui_id)
        while len(self.seen) > self.max_seen:
            self.seen.popitem(last=False)
        while len(self.hashes) > self.max_seen:
            self.hashes.popitem(last=False)
        # Preserve the last nonempty snapshot; an empty UI is not a new session.
        if current_ids:
            self.previous = current_ids
        return fresh


class Bridge:
    def __init__(self, api: ApiClient, window: FixedWindow, config: GroupConfig, send: bool):
        self.api = api
        self.window = window
        self.config = config
        self.send_enabled = send
        self.tracker = SessionTracker()
        self.attempted_outbound: set[str] = set()

    def get_config(self) -> GroupConfig:
        new = GroupConfig.from_dict(self.api.request("/api/bridge/config"))
        if new.identities() != self.config.identities():
            raise BridgeError("群或成员配置已变更；桥接停止，请核对后手动重启。")
        return new

    def initialize(self) -> None:
        self.window.open_once()
        self.tracker.observe(self.window.snapshot(), seed=True)
        self.api.heartbeat("dry_run", "已核对固定群并跳过启动时全部可见历史；尚未处理新消息。")

    def process_image(self, message_id: str, message, sender: str) -> None:
        payload = {"message_id": message_id, "group_name": self.config.group_name, "sender": sender}
        try:
            self.window.verify()
            if not self.get_config().live_enabled:
                return
            self.api.heartbeat("ready", "正在读取指定成员的新图片；识别结果仍须人工确认。")
            download = getattr(message, "download", None)
            if not callable(download):
                raise ImageTransferError("download_failed")
            with private_image_directory() as directory:
                try:
                    returned = download(dir_path=directory, original=False)
                except Exception:
                    # Recheck UI separately; a group change is always fatal.
                    self.window.verify()
                    raise ImageTransferError("download_failed") from None
                self.window.verify()
                data = read_downloaded_image(returned, directory)
                if not self.get_config().live_enabled:
                    return
                self.api.heartbeat("ready", "图片临时读取完成，准备本机识别处理。")
                # The server validates the image encoding and owns reply queues.
                # Never treat '[图片]' or a display label as a meaningful caption.
                self.api.request("/api/bridge/image", dict(payload,
                    image_base64=base64.b64encode(data).decode("ascii"), text=""))
        except (ImageTransferError, OSError) as exc:
            self.window.verify()
            if self.get_config().live_enabled:
                self.api.heartbeat("ready", "图片未能读取，将提示发送者重新提供。")
                error_code = exc.code if isinstance(exc, ImageTransferError) else "download_failed"
                self.api.request("/api/bridge/image-error", dict(payload, error=error_code))

    def tick(self) -> None:
        current_config = self.get_config()
        fresh = self.tracker.observe(self.window.snapshot())
        active = self.send_enabled and current_config.live_enabled
        if not active:
            # Absolutely no message body crosses the local API in dry run.
            self.api.heartbeat("dry_run", "固定群检查通过；仅检查，不上传正文、不记录、不发送。")
            return

        self.api.heartbeat("ready", "固定窗口会话模式运行；UI ID 仅本次会话有效，必须人工确认读数。")
        for message_id, message in fresh:
            kind = field(message, "type")
            if field(message, "attr") != "friend" or kind not in ("text", "image"):
                continue
            sender = field(message, "sender")
            if sender not in (self.config.patient_sender, self.config.caregiver_sender):
                continue
            text = field(message, "content")
            if kind == "text" and (not isinstance(text, str) or not text.strip() or len(text) > 3000):
                continue
            # Author's message.chat_info() is an additional identity check.
            chat_info = getattr(message, "chat_info", None)
            if callable(chat_info):
                info = chat_info()
                if (not isinstance(info, dict) or info.get("chat_type") != "group"
                        or info.get("chat_name") != self.config.group_name):
                    raise BridgeError("单条消息的来源群核验失败，已停止。")
            self.window.verify()
            if kind == "image":
                self.process_image(message_id, message, sender)
                continue
            # Model-backed answers can take tens of seconds. Refresh before
            # each serial request so several questions do not expire one
            # heartbeat cumulatively. A single hung operation still times out.
            self.api.heartbeat("ready", "指定群消息处理中；仍需人工核对和确认读数。")
            # Server owns reply queuing. Never send a returned 'reply' directly.
            self.api.request("/api/bridge/message", {
                "message_id": message_id, "group_name": self.config.group_name,
                "sender": sender, "text": text,
            })

        self.window.verify()
        self.api.heartbeat("ready", "固定群核验通过，准备逐条处理待发消息。")
        queue = self.api.request("/api/bridge/outbox")
        messages = queue.get("messages")
        if not isinstance(messages, list):
            raise BridgeError("待发队列格式异常，已停止。")
        for item in messages:
            if not isinstance(item, dict):
                raise BridgeError("待发消息格式异常，已停止。")
            item_id = item.get("id")
            if not isinstance(item_id, (str, int)) or isinstance(item_id, bool):
                raise BridgeError("待发消息缺少 ID，已停止。")
            item_key = str(item_id)
            if item_key in self.attempted_outbound:
                raise BridgeError("服务重复派发已尝试的消息；已停止，避免重复发送。")
            try:
                if item.get("group_name") != self.config.group_name:
                    raise BridgeError("待发消息目标不在唯一群白名单内。")
                text = item.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                    raise BridgeError("待发文本为空或超出长度限制。")
                if not self.get_config().live_enabled:
                    raise BridgeError("网页已关闭实际群发送；本次未继续发送。")
                self.window.verify()
                # Mark before any UI action. A send whose result is uncertain
                # must never be repeated by this session.
                self.attempted_outbound.add(item_key)
                self.window.send(text)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, BridgeError) else "微信操作异常，结果未确认；请人工检查。"
                self.api.request("/api/bridge/ack", {"id": item_id, "status": "failed", "error": reason})
                raise BridgeError(reason) from None
            # If ACK fails, stop. Never call send again even if delivery happened.
            self.api.request("/api/bridge/ack", {"id": item_id, "status": "sent"})


def read_token(path: Path) -> str:
    token = os.environ.get("HEALTH_BOT_BRIDGE_TOKEN")
    if token:
        return token.strip()
    try:
        raw = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        raise BridgeError("找不到桥接 token 文件。先启动网页，或使用 --token-file 指定 data/config.json。") from None
    if raw.startswith("{"):
        try:
            value = json.loads(raw).get("bridge_token")
        except (ValueError, AttributeError):
            raise BridgeError("token 文件不是有效配置。") from None
        if not isinstance(value, str):
            raise BridgeError("配置文件缺少 bridge_token。")
        return value.strip()
    return raw


def load_wechat():
    if sys.platform != "win32":
        raise BridgeError("可选微信桥接仅支持 Windows；网页程序不受此限制。")
    try:
        module = importlib.import_module("wxautox4")
    except ImportError:
        raise BridgeError("尚未安装可选 wxautox4。网页演示可正常使用；桥接依赖需单独核对授权和版本。") from None
    try:
        return module.WeChat()
    except Exception:
        raise BridgeError("无法连接已登录的微信桌面窗口；请核对客户端版本、桌面状态和 wxautox4 授权。") from None


@contextmanager
def single_instance(lock_path: Path):
    """Prevent two local bridge processes from driving the same WeChat UI."""
    if sys.platform != "win32":
        raise BridgeError("可选微信桥接仅支持 Windows；网页程序不受此限制。")
    import msvcrt
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise BridgeError("已有桥接进程运行，请先关闭原进程。") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="单个家庭微信群桥接，默认只检查、不收发正文。")
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--token-file", type=Path, default=Path(__file__).resolve().parent / "data" / "config.json")
    parser.add_argument("--send", action="store_true", help="请求实际收发；仍须网页 live_enabled 开启")
    parser.add_argument("--once", action="store_true", help="检查一次并退出；与 --send 不兼容")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.send and args.once:
        parser.error("--once 仅用于只读检查，不能和 --send 同用")
    if not 3 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds 必须在 3 到 60 秒之间")
    api = None
    try:
        with single_instance(Path(__file__).resolve().parent / "data" / "bridge.lock"):
            api = ApiClient(args.server, read_token(args.token_file))
            config = GroupConfig.from_dict(api.request("/api/bridge/config"))
            bridge = Bridge(api, FixedWindow(load_wechat(), config.group_name), config, args.send)
            bridge.initialize()
            print("固定群初始检查完成；启动前的可见历史已跳过。")
            print("实际模式：仅网页开关开启时收发；Ctrl+C 停止。" if args.send else "只检查模式：不上传消息正文、不发送微信；Ctrl+C 停止。")
            if args.once:
                return 0
            while True:
                bridge.tick()
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        detail = "桥接已由操作者停止；微信群不再受理新消息。"
        print(detail)
        if api:
            try:
                api.heartbeat("error", detail)
            except Exception:
                pass
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, BridgeError) else "桥接异常，已停止。请人工检查窗口和服务状态后重启。"
        print("未连接/已停止：" + detail, file=sys.stderr)
        if api:
            try:
                api.heartbeat("error", detail)
            except Exception:
                pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
