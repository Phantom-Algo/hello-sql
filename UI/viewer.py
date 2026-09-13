"""HELLO-SQL 本地查询流程查看器服务。

``InspectionViewer`` 在首次 ``/inspect`` 时懒启动一个只监听
``127.0.0.1`` 随机端口的 HTTP 服务，然后使用 Python 标准库
``webbrowser`` 打开系统默认浏览器。该方式同时适用 macOS、Windows
和 Linux，不引入 Electron 或外部 Web 服务。

查看器只暴露当前进程内的不可变 ``InspectionSnapshot``。每次 API
请求都通过注入的 provider 读取 TraceHub，不接收 SQL、不暴露文件系统、
不调用 Parser/Storage/Executor。静态页面从已安装的 ``UI/assets`` 读取，
因此安装后不依赖项目当前工作目录。
"""

from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from threading import RLock, Thread
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

from UI.inspection import InspectionSnapshot


SnapshotProvider = Callable[[str | None], InspectionSnapshot | None]
"""根据可选模块名读取最近快照的回调类型。"""

_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class _InspectionHttpServer(ThreadingHTTPServer):
    """在标准库 HTTP 服务器上保存只读快照 provider。

    ``daemon_threads`` 使正在读取页面的客户端不会阻止 HELLO-SQL 进程
    正常退出。服务器不保存 QueryTrace 副本，所有数据仍由 TraceHub 管理。
    """

    daemon_threads = True

    def __init__(self, provider: SnapshotProvider) -> None:
        """绑定本机随机端口并保存只读快照回调。"""

        self.snapshot_provider = provider
        super().__init__(("127.0.0.1", 0), _InspectionRequestHandler)


class _InspectionRequestHandler(BaseHTTPRequestHandler):
    """仅处理静态资源与最近追踪 JSON 的 GET 请求。"""

    server: _InspectionHttpServer

    def do_GET(self) -> None:
        """按 URL 路径分发页面资源或 ``/api/trace`` 只读数据。

        未知路径返回 JSON 404，无效模块返回 400，暂无查询时返回
        404。所有响应都禁止缓存并限制 MIME 探测。
        """

        parsed = urlsplit(self.path)
        if parsed.path == "/api/trace":
            self._serve_trace(parse_qs(parsed.query).get("module", [None])[0])
            return
        asset = _ASSETS.get(parsed.path)
        if asset is None:
            self._send_json(404, {"error": "not_found"})
            return
        self._serve_asset(*asset)

    def _serve_trace(self, module: str | None) -> None:
        """读取指定 A/B/C/ALL 快照并返回 JSON。

        provider 的 ValueError 表示用户传入未知模块；其他异常不向页面
        泄露内部信息，统一返回 500。
        """

        try:
            snapshot = self.server.snapshot_provider(module)
        except ValueError as error:
            self._send_json(400, {"error": "invalid_module", "message": str(error)})
            return
        except Exception:
            self._send_json(500, {"error": "snapshot_unavailable"})
            return
        if snapshot is None:
            self._send_json(404, {"error": "no_trace"})
            return
        self._send_json(200, snapshot.to_dict())

    def _serve_asset(self, asset_name: str, content_type: str) -> None:
        """从已安装 UI 包读取一个白名单静态文件。

        资源名只能来自模块常量 ``_ASSETS``，浏览器无法通过 URL 提供
        任意文件路径。打包缺失资源时返回不泄露本地路径的 500。
        """

        try:
            payload = files("UI").joinpath("assets", asset_name).read_bytes()
        except (FileNotFoundError, OSError):
            self._send_json(500, {"error": "asset_unavailable"})
            return
        self._send_bytes(200, payload, content_type)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        """将字典编码为保留中文的 UTF-8 JSON 响应。"""

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(status, encoded, "application/json; charset=utf-8")

    def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        """发送含安全响应头和精确长度的字节内容。"""

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        """关闭 BaseHTTPRequestHandler 默认的 stderr 访问日志。

        查看器请求是界面内部行为，若输出到 REPL 会打断用户当前
        编辑行。这里只关闭常规日志，HTTP 错误仍通过状态码返回页面。
        """

        del format, args


class InspectionViewer:
    """管理本地查看器的懒启动、URL 构建、浏览器打开和关闭。"""

    def __init__(self, provider: SnapshotProvider) -> None:
        """保存快照 provider，但不立即占用端口或创建线程。"""

        if not callable(provider):
            raise TypeError("InspectionViewer provider must be callable")
        self._provider = provider
        self._server: _InspectionHttpServer | None = None
        self._thread: Thread | None = None
        self._lock = RLock()

    @property
    def running(self) -> bool:
        """返回本地 HTTP 线程是否已启动且仍然存活。"""

        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> str:
        """懒启动查看器并返回不含筛选参数的根 URL。

        多次调用会复用同一个服务器和端口。创建与线程启动在可重入锁中
        完成，避免并发 ``/inspect`` 启动多个窗口后端。
        """

        with self._lock:
            if self._server is None or self._thread is None or not self._thread.is_alive():
                server = _InspectionHttpServer(self._provider)
                thread = Thread(
                    target=server.serve_forever,
                    name="hello-sql-inspector",
                    daemon=True,
                )
                thread.start()
                self._server = server
                self._thread = thread
            host, port = self._server.server_address
            return f"http://{host}:{port}/"

    def url(self, module: str = "ALL") -> str:
        """构建包含巵3选模块的本地查看器 URL。

        参数通过 ``urlencode`` 生成，页面再以同一值请求 JSON API，不会
        插入到 HTML 模板。模块有效性由 QueryInspector 统一验证。
        """

        return f"{self.start()}?{urlencode({'module': module})}"

    def open(self, module: str = "ALL") -> tuple[str, bool]:
        """打开系统默认浏览器并返回 URL 与打开结果。

        ``webbrowser.open`` 返回 False 或抛 ``webbrowser.Error`` 时，本地服务
        仍保持可用，调用方可以把 URL 显示给用户手动打开。
        """

        target = self.url(module)
        try:
            opened = bool(webbrowser.open(target, new=2))
        except webbrowser.Error:
            opened = False
        return target, opened

    def close(self) -> None:
        """关闭已启动的本地服务并释放端口。

        尚未启动或已关闭时调用没有副作用。先在锁内取出并清空引用，
        再在锁外执行 shutdown/join，避免服务线程等待时阻塞其他状态读取。
        """

        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)

