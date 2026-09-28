"""服务端 i18n —— 让首屏 HTML 直接是正确语言,消掉"先英文后中文"的闪烁。

**问题**:语言原本只存在浏览器的 ``localStorage`` 里,服务端不知道。所以渲染出来
的 ``data-i18n`` 元素只能填**默认语言(英文)**,再由 ``DOMContentLoaded`` 里的 JS
改成中文 —— 改的时机在首屏之后,于是每次整页导航都闪一下。

**做法**:语言同时写进 cookie,服务端渲染时就用它取词。首屏直接是对的语言,
不需要任何"先渲染再纠正"。

**词典的真源**是 ``web/static/js/i18n-data.js`` —— 一个合法 JSON 载荷外面包一行
``window.__I18N__ = …``:

    * 浏览器把它当**普通脚本**同步加载(带 ``?v=`` 缓存,不会每页重下)
    * 这里剥掉包装后 ``json.loads``,拿到的就是同一份数据

两边读同一份文件,所以**不会分叉**。改词条只改那一个文件。
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware

# cookie 名与前端 localStorage 的键保持一致,便于两边对照排查。
LANG_COOKIE = "lang"
LANGUAGES = ("zh", "en")
# 没选过语言时用哪个。刻意保持 **en** —— 与前端原来的行为一致(切语言前的默认),
# 不借这次改动偷偷改变默认观感。要改成 zh 的话两边一起改(i18n.js 的 currentLang)。
DEFAULT_LANG = "en"

_DICT_PATH = (Path(__file__).resolve().parent.parent
              / "web" / "static" / "js" / "i18n-data.js")


def _load() -> dict[str, dict[str, str]]:
    """读 ``i18n-data.js`` 并把 JSON 载荷解析出来。

    解析失败**不抛异常**:页面照常起得来,只是文案退回 key 本身 —— 比整个服务
    因为一个词典文件挂掉好。真要查的话看 ``data`` 是不是空的。
    """
    try:
        raw = _DICT_PATH.read_text(encoding="utf-8")
        payload = raw[raw.index("{"):raw.rindex("}") + 1]
        data = json.loads(payload)
    except (OSError, ValueError, IndexError):
        return {}
    return data if isinstance(data, dict) else {}


DATA: dict[str, dict[str, str]] = _load()


def normalize(lang: str | None) -> str:
    """把任意输入收敛成一个可用语言码。认不出来就回默认。"""
    value = str(lang or "").strip().lower()
    return value if value in DATA else DEFAULT_LANG


_current: ContextVar[str] = ContextVar("egodata_lang", default=DEFAULT_LANG)


def get_current_lang() -> str:
    return _current.get()


def t(key: str, lang: str | None = None) -> str:
    """取词。**取不到就返回 key 本身** —— 页面上会显示出一个明显的
    ``nav_foo`` 而不是空白,一眼看得出是漏翻,比静默空着好排查。"""
    use = normalize(lang) if lang is not None else _current.get()
    table = DATA.get(use) or DATA.get(DEFAULT_LANG) or {}
    return table.get(key) or key


class LanguageMiddleware(BaseHTTPMiddleware):
    """从 cookie 读语言,放进 ContextVar —— Jinja 里注册的 ``t`` 直接用。

    用 ContextVar 而不是往每个 ``TemplateResponse`` 的 context 里塞:后者要改
    20 多处调用点,而 ``t`` 是**每个模板都要用**的东西,漏一个就是一片英文。
    """

    async def dispatch(self, request, call_next):
        token = _current.set(normalize(request.cookies.get(LANG_COOKIE)))
        try:
            return await call_next(request)
        finally:
            _current.reset(token)
