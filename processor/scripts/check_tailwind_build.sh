#!/usr/bin/env bash
# 检查 web/static/tailwind-generated.css 是否与当前源码同步。
#
# 为什么需要这个:那份 CSS 是**构建产物**,只包含生成时源码里出现过的类。
# 往 templates/ 或 static/js/ 里写了一个没被用过的 Tailwind 类(比如把 p-6
# 改成 px-6 pt-3 pb-6),类就**静默失效** —— 页面照常显示,只是样式没上,
# 而且不报任何错。已经踩过两次:
#   第一次:p-6 拆成 px-6 pt-3 pb-6,左右内边距直接归零,内容贴到侧边栏上
#   第二次:训练页一半的类(w-28 / text-[10px] / accent-blue-500 / divide-y)
#           因为 CSS 停在几周前而全部失效
#
# 做法:用同样的 content 范围重新生成一份到临时文件,和现场那份比对。
# 有差异 = 忘了重新生成(或改了 content 范围)。
#
# 用法:
#   bash scripts/check_tailwind_build.sh          # 只检查
#   bash scripts/check_tailwind_build.sh --fix    # 检查并就地重新生成

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB="$(dirname "$HERE")/web"
OUT="$WEB/static/tailwind-generated.css"
INPUT="$WEB/static/tailwind-input.css"
CONTENT="$WEB/templates/**/*.html,$WEB/static/js/**/*.js"
CLI="$WEB/workflow-studio/node_modules/.bin/tailwindcss"

if [ ! -x "$CLI" ]; then
    echo "找不到 tailwindcss CLI:$CLI"
    echo "先跑 cd web/workflow-studio && npm install"
    exit 2
fi

TMP="$(mktemp -t tw-check-XXXXXX.css)"
trap 'rm -f "$TMP"' EXIT

"$CLI" -i "$INPUT" -o "$TMP" --content "$CONTENT" >/dev/null 2>&1

if cmp -s "$TMP" "$OUT"; then
    echo "tailwind-generated.css 与源码同步 ✓"
    exit 0
fi

if [ "${1:-}" = "--fix" ]; then
    cp "$TMP" "$OUT"
    echo "已重新生成 tailwind-generated.css"
    # 缓存键是 **web/VERSION**(前端号),不是 app/version.py 的后端号 ——
    # 模板里写的是 ``?v={{ asset_version }}``,而 asset_version 取自
    # app/web_version.py 读的 web/VERSION。1.19.4 起就是这么分的;这里原本
    # 还写着"bump app/version.py",照着做会 bump 一个跟缓存无关的文件,
    # 然后浏览器继续用旧 CSS。
    echo "⚠ 记得 bump web/VERSION —— 模板里的 ?v={{ asset_version }} 读的是它,"
    echo "  而且它**要重启服务才生效**(app/web_version.py 是 lru_cache 读一次)。"
    exit 0
fi

echo "❌ tailwind-generated.css 与源码不一致 —— 有类的样式没生效。"
echo "   跑 bash scripts/check_tailwind_build.sh --fix 重新生成"
exit 1
