"""模型后端配置 —— 云优先，本地兜底（零第三方依赖）

设计要点：
1. 默认走云（DeepSeek 的 OpenAI 兼容端点），更适合现场答辩，响应快、会说人话；
2. 云没有配 key、或云端请求失败（断网 / 余额不足），自动切回本地 Ollama，并**不崩溃**；
3. 只用一个极简 .env 解析器，不依赖 python-dotenv，保证 `python app.py` 在任何机器都能跑。

对外主要 API：
    current()            -> 当前生效的 Backend
    switch_local(reason) -> 切到本地，返回旧 Backend（已切过则返回 None）
    announce()           -> 打印启动那一行「模型：xxx（云/本地）」
    backend_label()      -> 给 /api/state 用的短标签
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"


def _load_env(path=ENV_PATH):
    """把 .env 里的键值塞进 os.environ（已存在的环境变量不覆盖，方便临时覆盖）。"""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env()


class Backend:
    """一次生效的模型后端配置。"""

    def __init__(self, provider, base_url, model, api_key):
        self.provider = provider          # "cloud" / "local"
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or ("ollama" if provider == "local" else "")

    @property
    def is_cloud(self):
        return self.provider == "cloud"

    @property
    def label(self):
        return f"{self.model}（{'云' if self.is_cloud else '本地'}）"

    def as_dict(self):
        return {"provider": self.provider, "model": self.model,
                "base_url": self.base_url, "label": self.label}


def _cloud():
    return Backend(
        "cloud",
        os.getenv("CLOUD_BASE_URL", "https://api.deepseek.com/v1"),
        os.getenv("CLOUD_MODEL", "deepseek-chat"),
        os.getenv("CLOUD_API_KEY", "").strip(),
    )


def _local():
    return Backend(
        "local",
        os.getenv("LOCAL_BASE_URL", "http://localhost:11434/v1"),
        os.getenv("LOCAL_MODEL", "qwen2.5:7b-instruct"),
        "ollama",
    )


def local_model_names():
    """本机 Ollama 实际装了的模型名（取不到返回空列表）。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(_local().base_url.replace("/v1", "") + "/api/tags", timeout=2) as r:
            doc = json.loads(r.read().decode("utf-8"))
        return [m.get("name", "") for m in doc.get("models", [])]
    except Exception:
        return []


def _pick_local_model(preferred):
    """配置里的本地模型没下载时，自动退到本机真实存在的一个（避免死路）。"""
    names = local_model_names()
    if not names:
        return preferred
    if preferred in names:
        return preferred
    for n in names:                       # 本机实测可用的 32k 版本优先
        if "qwen05b" in n.lower():
            return n
    for n in names:
        if "qwen" in n.lower():
            return n
    return names[0]


# 当前生效后端（进程内单例，可在运行时切换）
_CURRENT = None
_SWITCH_NOTES = []   # 给界面看的切换原因


def current():
    global _CURRENT
    if _CURRENT is None:
        want = os.getenv("MODEL_PROVIDER", "cloud").strip().lower()
        if want == "cloud":
            b = _cloud()
            if not b.api_key:
                _red("⚠  未配置 CLOUD_API_KEY，已回退本地模型")
                _SWITCH_NOTES.append("未配置 CLOUD_API_KEY，已回退本地模型")
                b = _local()
        else:
            b = _local()
        if not b.is_cloud:
            b.model = _pick_local_model(b.model)
        _CURRENT = b
    return _CURRENT


def switch_local(reason=""):
    """云端不可用时切本地。返回切换前的后端；已经是本地则返回 None。"""
    global _CURRENT
    b = current()
    if not b.is_cloud:
        return None
    old = b
    local = _local()
    local.model = _pick_local_model(local.model)
    _CURRENT = local
    _SWITCH_NOTES.append(f"{reason}，已切本地模型")
    _red(f"⚠  {reason}，已切本地模型（{local.model}）")
    return old


def notes():
    return list(_SWITCH_NOTES)


def backend_label():
    return current().label


# ------------------------- 控制台彩色 -------------------------
def _color(text, code):
    if os.getenv("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _red(text):
    print(_color(text, "31"), flush=True)


def red(text):
    print(_color(text, "31"), flush=True)


def green(text):
    print(_color(text, "32"), flush=True)


def announce(prefix="模型"):
    """启动时必须打印的那一行。"""
    b = current()
    line = f"{prefix}：{b.label}"
    if b.is_cloud:
        print(line, flush=True)
    else:
        print(line, flush=True)
    return line


if __name__ == "__main__":
    announce()
    print(current().as_dict())
    print("本地已装模型：", local_model_names())
