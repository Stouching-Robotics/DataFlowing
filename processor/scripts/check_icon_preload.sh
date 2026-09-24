#!/bin/bash
# 校验:代码中引用的 iconify 图标是否全部在预加载文件里。
# 缺失的图标会在运行时请求 api.iconify.design → 页面图标闪烁/空白。
# 用法: scripts/check_icon_preload.sh ;退出码 0 = 齐全,1 = 有缺失。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PRE="$ROOT/web/static/iconify-preload.js"
[ -f "$PRE" ] || { echo "找不到 $PRE"; exit 1; }

# 代码里所有 "前缀:图标名" 引用(ant-design:xxx 等)。
#
# ★ app/processing/modules/ 也必须扫 —— 工作流节点的 icon 是在 Python 里声明的
#   (如 data_cleaning.py 的 ``icon = "ant-design:check-circle-outlined"``),
#   这些图标和前端硬编码的一样会被调色板渲染。曾经漏扫这里,导致新增节点
#   用了白名单外的图标而检查脚本仍报"全部已预加载"。
#
#   Python 文件里会有 ``cuda:0`` / ``-c:v`` 之类的噪声,但下面只认 ant-design:
#   前缀,噪声会被过滤掉。
USED="$(grep -rhoE "[a-z0-9-]+:[a-z0-9-]+" \
  "$ROOT/web/workflow-studio/src" "$ROOT/web/static/js" "$ROOT/web/templates" \
  "$ROOT/app/processing/modules" 2>/dev/null \
  | sort -u)"

MISSING=""
while IFS= read -r ref; do
  # 只检查 ant-design 前缀(预加载文件目前只包含 ant-design)
  case "$ref" in
    ant-design:*) ;;
    *) continue ;;
  esac
  name="${ref#ant-design:}"
  if ! grep -q "'${name}': { body" "$PRE"; then
    # 指出是谁声明的,便于补齐
    WHERE="$(grep -rlE "[\"']?${ref}[\"']?" \
      "$ROOT/web/workflow-studio/src" "$ROOT/web/static/js" "$ROOT/web/templates" \
      "$ROOT/app/processing/modules" 2>/dev/null \
      | sed "s|^$ROOT/||" | paste -sd, -)"
    MISSING="$MISSING"$'\n'"  $ref    ← ${WHERE:-未知位置}"
  fi
done <<< "$USED"

if [ -n "$MISSING" ]; then
  echo "以下图标未预加载(iconify-icon 会去 api.iconify.design 拉,页面卡顿/空白)。"
  echo "请改用 $PRE 白名单里的图标,或把图标数据补进去:"
  echo "$MISSING"
  exit 1
fi
echo "全部引用图标均已预加载 ✓"
