"""前端版本号 —— 读 ``processor/web/VERSION`` 的第一行。

一个号,两个用途:

    1. 模板里的 ``?v=`` 静态资源缓存键 —— 改了 JS/CSS 就 bump 它,浏览器才会重新拉
    2. 提交信息里的「前端 X」

**为什么不让后端号来当缓存键**(那是原来的做法):那样每次改前端都要 bump
*两个*号 —— 后端号给 ``?v=`` 用、前端号给提交信息用 —— 而且两个数对不上时,
看提交信息的人没法判断浏览器该拿哪一版。现在改前端只动一个数。

★ 读一次就缓存住(``lru_cache``),不是每请求读盘。代价是**改完 ``web/VERSION``
  要重启服务才生效** —— 与后端号原来的行为一致(它也是模块级常量)。真要让
  bump 立刻生效,得把它做成可调用对象、模板里写 ``{{ asset_version() }}``,
  那就得改 22 处模板,不划算。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "web" / "VERSION"


@lru_cache(maxsize=1)
def frontend_version() -> str:
    """``web/VERSION`` 的第一行(去空白)。文件缺失/为空时返回 ``"0"``。

    读不出来**不抛异常**:静态资源照样能服务,只是 ``?v=`` 退化成一个常量 ——
    页面完全可用,代价仅仅是缓存可能不新鲜。为了让整个站点因为一个版本文件
    挂掉,不值得。
    """
    try:
        first_line = VERSION_FILE.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return "0"
    return first_line.strip() or "0"
