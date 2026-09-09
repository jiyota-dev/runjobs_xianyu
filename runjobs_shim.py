"""RunJobs 平台工具的本地替身，用于在平台之外执行 provision.py。

平台执行初始化脚本时会自动注入 call_tool() 与 server_tool() 两个全局函数。
在沙盒终端里手动 `python3 provision.py`、或在本机调试时它们都不存在，脚本会直接
NameError 退出 —— 于是每改一行都得走「粘贴到控制台 → 重启容器」，一轮 5~10 分钟。
本模块提供等价行为的替身，让同一份 provision.py 在三种环境下都能跑。

设计原则（这个项目已经在静默失败上栽过好几次，所以定死）：

1. **能等价实现的就真的实现**。call_tool 的 11 个工具操作的是沙盒自身
   （文件读写、exec、下载），在终端和本机都有真实语义，照做即可。
2. **实现不了的必须明确报错**，绝不返回假的成功值。server_tool 的多数能力
   （发消息、日程、记忆、生成文件、硬件管理）依赖平台后端，本地无从模拟；
   静默返回一个像样的假结果，只会让调用方以为成功，把问题推到更远的地方。
3. **行为有差异的要说出来**。比如本地的 create_port_preview 给的是
   http://localhost:PORT，不是公网 URL，日志里必须讲清楚。

用法（provision.py 开头）::

    if "call_tool" not in globals():
        from runjobs_shim import call_tool, server_tool
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

__all__ = ["call_tool", "server_tool", "ShimUnavailable"]

_PREFIX = "[shim]"


class ShimUnavailable(RuntimeError):
    """该工具依赖 RunJobs 平台后端，在平台之外无法提供等价实现。"""


def _note(msg: str) -> None:
    # 直接写 stderr：provision.py 的 log() 会把 stdout 收进 init.log，
    # 而 shim 的提示属于「运行环境不同」的元信息，不该混进正式日志。
    print(f"{_PREFIX} {msg}", file=sys.stderr)


def _need(kwargs: dict, key: str) -> Any:
    if key not in kwargs:
        raise TypeError(f"{_PREFIX} 缺少必需参数: {key}")
    return kwargs[key]


# ---------------------------------------------------------------- call_tool

def _t_exec(**kw) -> str:
    """执行命令。detach=True 时脱离当前会话常驻（对应平台的 detach 语义）。"""
    command = _need(kw, "command")
    if kw.get("detach"):
        # setsid + 重定向，确保父进程退出后依然存活；与平台 detach 的可观察行为一致
        subprocess.Popen(
            f"setsid nohup {command} < /dev/null > /dev/null 2>&1 &",
            shell=True, start_new_session=True,
        )
        return f"{_PREFIX} detached: {command}"
    p = subprocess.run(command, shell=True, capture_output=True, text=True)
    return (p.stdout or "") + (p.stderr or "")


def _t_read_file(**kw) -> str:
    return Path(_need(kw, "path")).read_text(encoding=kw.get("encoding", "utf-8"))


def _t_write_file(**kw) -> str:
    path = Path(_need(kw, "path"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_need(kw, "content"), encoding=kw.get("encoding", "utf-8"))
    return str(path)


def _t_edit_file(**kw) -> str:
    path = Path(_need(kw, "path"))
    old, new = _need(kw, "old_string"), _need(kw, "new_string")
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise ValueError(f"{_PREFIX} edit_file: 未找到待替换内容: {old[:60]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return str(path)


def _t_list_dir(**kw) -> list[str]:
    return sorted(p.name for p in Path(kw.get("path", ".")).iterdir())


def _t_delete_file(**kw) -> str:
    path = Path(_need(kw, "path"))
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    return str(path)


def _t_download_file(**kw) -> str:
    url, dest = _need(kw, "url"), Path(_need(kw, "path"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)
    return str(dest)


def _t_web_fetch(**kw) -> str:
    with urllib.request.urlopen(_need(kw, "url"), timeout=kw.get("timeout", 30)) as r:
        return r.read().decode("utf-8", "replace")


def _t_search_text(**kw) -> list[str]:
    """在目录下按子串搜索，返回 'path:lineno: line' 列表。"""
    pattern, root = _need(kw, "pattern"), Path(kw.get("path", "."))
    hits: list[str] = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if pattern in line:
                    hits.append(f"{f}:{n}: {line.strip()}")
        except OSError:
            continue
    return hits


_CALL_TOOLS = {
    "exec": _t_exec,
    "read_file": _t_read_file,
    "write_file": _t_write_file,
    "edit_file": _t_edit_file,
    "list_dir": _t_list_dir,
    "delete_file": _t_delete_file,
    "download_file": _t_download_file,
    "web_fetch": _t_web_fetch,
    "search_text": _t_search_text,
}

# 需要平台托管的浏览器会话，本地没有对应物
_CALL_TOOLS_UNAVAILABLE = {"open_browser", "screenshot"}


# -------------------------------------------------------------- server_tool

def _s_create_port_preview(**kw) -> str:
    port = _need(kw, "port")
    # 行为差异必须说明：本地拿到的是回环地址，不是平台那种公网 URL。
    # 调用方若把它当公网地址用（写进说明、发给用户），会得到一个别人打不开的链接。
    _note(f"create_port_preview: 本地无公网暴露能力，返回回环地址（port={port}）")
    return f"http://localhost:{port}"


def _s_list_port_previews(**kw) -> str:
    _note("list_port_previews: 本地无端口预览记录，返回空")
    return ""


def _s_delete_port_preview(**kw) -> str:
    _note(f"delete_port_preview: 本地无需删除（id={kw.get('id')}）")
    return "ok"


def _s_send_message(**kw) -> str:
    """把消息打到 stderr。平台上它会进工作空间聊天，本地只能让人看见。

    注意：实测在**平台的初始化脚本里**调用 send_message 同样会被拒
    （tool "send_message" is not available for agent-side calls），
    所以别把它当成可靠的用户触达手段——重要信息要落盘到用户能翻到的文件。
    """
    body = kw.get("message", "")
    _note("send_message（本地仅打印，不会送达任何人）:")
    for line in str(body).splitlines():
        print(f"{_PREFIX}   {line}", file=sys.stderr)
    return "ok"


def _s_report_progress(**kw) -> str:
    _note(f"report_progress: {kw.get('message', '')}")
    return "ok"


_SERVER_TOOLS = {
    "create_port_preview": _s_create_port_preview,
    "list_port_previews": _s_list_port_previews,
    "delete_port_preview": _s_delete_port_preview,
    "send_message": _s_send_message,
    "report_progress": _s_report_progress,
}


# ------------------------------------------------------------------ 对外接口

def call_tool(name: str, **kwargs) -> Any:
    if name in _CALL_TOOLS_UNAVAILABLE:
        raise ShimUnavailable(
            f"{_PREFIX} call_tool('{name}') 需要平台托管的浏览器会话，本地无等价实现"
        )
    fn = _CALL_TOOLS.get(name)
    if fn is None:
        raise ShimUnavailable(
            f"{_PREFIX} call_tool('{name}') 尚未在 shim 中实现。"
            f" 已实现: {', '.join(sorted(_CALL_TOOLS))}"
        )
    return fn(**kwargs)


def server_tool(name: str, **kwargs) -> Any:
    fn = _SERVER_TOOLS.get(name)
    if fn is None:
        # 其余 20+ 个 server_tool（日程、记忆、生成 PDF/DOCX、邮件、硬件管理……）
        # 都依赖平台后端，没有诚实的本地实现。抛错而不是返回假成功——后者会让
        # 调用方以为事情办成了，把问题推到更难查的地方。
        raise ShimUnavailable(
            f"{_PREFIX} server_tool('{name}') 依赖 RunJobs 平台后端，本地无等价实现。"
            f" 已提供降级的: {', '.join(sorted(_SERVER_TOOLS))}"
        )
    return fn(**kwargs)


def is_active() -> bool:
    """当前是否在用 shim（供脚本决定要不要跳过平台专属步骤）。"""
    return True


if __name__ == "__main__":
    _note("自检：")
    assert call_tool("exec", command="echo hi").strip() == "hi"
    p = Path(os.environ.get("TMPDIR", "/tmp")) / ".runjobs_shim_selfcheck"
    call_tool("write_file", path=str(p), content="x")
    assert call_tool("read_file", path=str(p)) == "x"
    call_tool("delete_file", path=str(p))
    assert not p.exists()
    assert server_tool("create_port_preview", port=8089) == "http://localhost:8089"
    for bad, fn in (("open_browser", call_tool), ("save_memory", server_tool)):
        try:
            fn(bad)
        except ShimUnavailable:
            pass
        else:
            raise AssertionError(f"{bad} 应当抛 ShimUnavailable")
    _note("自检通过")
