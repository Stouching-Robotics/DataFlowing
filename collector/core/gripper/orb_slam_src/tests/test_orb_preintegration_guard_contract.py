#!/usr/bin/env python3
"""静态安全契约：`mpImuPreintegrated` 的每一处解引用都必须判空。

2026-09-23 的 SIGSEGV（`fault_addr=0x8b8`）回溯为
`IMU::Preintegrated::SetNewBias` ← `Optimizer::InertialOptimization`
← `LocalMapping::InitializeIMU+0x87b` ← `LocalMapping::Run+0x66e`，
原生日志在 `[FATAL_SIGNAL]` 前一行正是 `Not preintegrated measurement`。

根因不是「多线程竞态」，而是一句**只打印不跳过**的判空：

    if(!pKFi->mpImuPreintegrated)
        std::cout << "Not preintegrated measurement" << std::endl;

    pKFi->mpImuPreintegrated->SetNewBias(...);   // ← 下一行照样解引用

打印出来说明**这个状态真会发生**，而代码什么都没做。空预积分是设计内的合法态
（`Tracking.cc` 里 IMU 空档期建的关键帧就写 `mpImuPreintegrated=NULL`），
所以这不是「异常输入」，是「没处理的分支」。

同一族本轮共查出 5 处，形态各不相同（只打印不跳过 / 连打印都没有 / 只看
`bImu` / 赋值派生 / 清理路径），所以本测试**不逐条点名**，而是验证那条通用
不变量——「任何解引用都必须被某处判空支配」。四种判定规则对应树里真实存在的
四种写法：

  R0 同行短路：`!pFrame->mpImuPreintegrated || !(pFrame->mpImuPreintegrated->dT > 0.f)`
  R1 外层条件：解引用所在的 `if/for/while` 条件里提到了同一对象
  R2 跳过式守卫：前面有 `if(!<对象>)`，其语句体（块**或单条语句**，
     花括号可以写在下一行）以 continue/return 结束，中间没有再赋值，
     且**与解引用同层支配**（光判「守卫体结束在解引用之前」不够，
     见 `_dominates`）
  R3 正向守卫：前面有 `if(<对象>)` 且紧接的就是本条语句（无花括号写法）

扫描器本身也要反向验证（`test_scanner_keeps_its_teeth`）：把本轮的 5 处
修复逐一改回原样，扫描器必须变红。理由是最下面那条 R2 曾被证伪过一次 ——
第一版扫描器在这 5 处里对 `LocalMapping` 那处**毫无反应**，因为更早的计数
循环里有个同名的守卫替它「顶」了，只是那个块早就闭合了。全绿的扫描器
不等于有牙的扫描器。

为什么不用「往回数 N 行」：第一版审计脚本那样做，在 `FullInertialBA` 上把
**已被外层判空支配**的 `C.block(9,9)` 误报成危险（判空在 60 行外），又在
`LocalMapping` 上漏掉了正向写法 `if(keyFrame->mpImuPreintegrated)`。两种误判
都会让人去改本来正确的代码。

扫描前先剥掉注释与字符串字面量：本次修复自己写的注释里就含
`mpImuPreintegrated` 字样，不剥注释的扫描器会被自己骗过。

原生侧的行为验证在 tests/native/（ctest 名见 CMakeLists）。
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "ORB-SLAM/src"
INCLUDE = ROOT / "ORB-SLAM/include"

MEMBER = r"mpImuPreintegrated(?:Frame)?"

# 解引用点：<链>->mpImuPreintegrated->  或  <链>.mpImuPreintegrated->
# 链**按结构**拼（基址 + 若干个成员步），不能用扁平字符类：把 `(` 和 `\w`
# 放进同一个类里，`(pFrame->mpImuPreintegrated` 会被当成「类匹配到 (pFrame」
# 而整串吞掉，身份随之变成 `(pFrame->mpImuPreintegrated`，与守卫对不上；
# 反过来若不含 `(`，`(*itKF)->mpImuPreintegrated` 从 `)` 起抓又变成
# `itKF)`（`\w` 吃掉了 itKF）。两种错法都真发生过。
_BASE = r"(?:\(\s*\*\s*\w+\s*\)|\w+)"
_STEP = r"(?:->|\.)\s*\w+"
DEREF = re.compile(rf"({_BASE}(?:\s*{_STEP})*)\s*(->|\.)\s*({MEMBER})\s*->")


def _identity(chain, sep, member):
    """只取链的最后一段做身份：守卫也写作同样的末段（见模块 docstring）。"""
    last = re.split(r"(?:->|\.)", chain)[-1].strip()
    return f"{last}{sep}{member}"


def _mentions(text, ident):
    """text 里是否提到该对象。

    用 `(?<!\\w)`/`(?!\\w)` 而不是 `\\b`：身份可能以 `(` 开头
    （`(*itKF)->mpImuPreintegrated`），而 `(` 与它前面的 `!` 之间没有词边界，
    `\\b(` 永远匹配不上。同时这个写法仍能挡住 `xmpImuPreintegrated` 这种后缀误配。
    """
    return re.search(rf"(?<!\w){re.escape(ident)}(?!\w)", text) is not None


def _strip_comments_and_strings(lines):
    """剥掉 // 注释、/* */ 注释与字符串字面量，其余原样（够用的精度即可）。"""
    out, in_block = [], False
    for line in lines:
        buf, i, quote = [], 0, None
        while i < len(line):
            two = line[i:i + 2]
            if in_block:
                if two == "*/":
                    in_block = False
                    i += 2
                else:
                    i += 1
                continue
            ch = line[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                i += 1
                continue
            if two == "//":
                break
            if two == "/*":
                in_block = True
                i += 2
                continue
            if ch in "\"'":
                quote = ch
                i += 1
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return out


def _block_end(lines, brace_line, brace_col):
    """从 lines[brace_line][brace_col] 的 '{' 起配平，返回 (结束行, 结束列)。"""
    depth = 0
    for r in range(brace_line, len(lines)):
        start = brace_col if r == brace_line else 0
        for c in range(start, len(lines[r])):
            if lines[r][c] == "{":
                depth += 1
            elif lines[r][c] == "}":
                depth -= 1
                if depth == 0:
                    return r, c
    return None, None


def _condition_above(lines, brace_line, brace_col):
    """返回开启该 '{' 的控制语句条件文本（找不到返回 None）。

    条件可能跨行，必须**整段拼起来**再返回：`if(a && b &&` 换行 `c)` 这种写法
    下，只返回第一行会得到不含 `c` 的残句，把已被支配的解引用误判成没守卫
    （LocalMapping::KeyFrameCulling 的预积分判空正是这种写法）。
    """
    same = lines[brace_line][:brace_col]
    r = brace_line
    while r >= 0 and brace_line - r <= 6:
        candidate = same if r == brace_line else lines[r]
        if re.search(r"\b(if|for|while|else)\b", candidate):
            joined = "\n".join(lines[r:brace_line] + [same])
            return " ".join(joined.split())
        r -= 1
    return None


def _enclosing_conditions(lines, index):
    """包住 lines[index] 的所有控制语句条件（由内到外）。"""
    conds, depth = [], 0
    r = index
    while r >= 0:
        line = lines[r]
        for c in range(len(line) - 1, -1, -1):
            ch = line[c]
            if ch == "}":
                depth += 1
            elif ch == "{":
                if depth == 0:
                    cond = _condition_above(lines, r, c)
                    if cond:
                        conds.append(cond)
                else:
                    depth -= 1
        r -= 1
    return conds


def _condition_end(lines, cond_line, limit=6):
    """条件文本的结束行：从 cond_line 起数圆括号，配平的那一行。

    没有它就会把条件的续行当成语句：`if (!a || !b ||` 换行 `!c)` 换行 `{`
    这种写法下，`_statement_body` 会认第 2 行为「单条语句」，于是那个真正
    的 `{` 块永远找不到，守卫体里也就找不到 return。
    """
    bal = 0
    for r in range(cond_line, min(len(lines), cond_line + limit)):
        bal += lines[r].count("(") - lines[r].count(")")
        if bal <= 0:
            return r
    return cond_line


def _brace_depths(lines):
    """每行**行首**的花括号嵌套深度（剥注释后按行数，够用的精度即可）。"""
    depths, d = [], 0
    for line in lines:
        depths.append(d)
        d += line.count("{") - line.count("}")
    return depths


def _dominates(depths, guard_line, body_end, index):
    """跳过式守卫是否**支配** index：同层，且中途没有退出那一层。

    只用「守卫体结束在解引用之前」判是不够的 —— 那等于说「文件里前面某处
    有个 continue」，跟这个解引用在不在它辖内毫无关系。清理路径里那个
    `if(!keyFrame->mpImuPreintegrated || ...) { ...; break; }` 属于更早的
    计数循环、块早就闭合了，却会「保护」到 80 行以外、跨了两个 for 的解引用：
    反向验证（真的把守卫摘掉）时正是它把红挡住了。
    """
    d = depths[guard_line]
    if depths[index] != d:
        return False
    return min(depths[body_end:index + 1]) >= d


def _scope_start(lines, index):
    """最近的上一个顶格行 —— 本树函数定义都顶格，用它给回看划界。

    不划界就得靠「往回数 N 行」：N 小了会漏掉函数开头就设好的早退守卫
    （PoseInertialOptimizationLastKeyFrame 的守卫在解引用前 200 行开外），
    N 大了又会把上一个函数里的同名守卫算进来。
    """
    for r in range(index - 1, -1, -1):
        if lines[r] and not lines[r][0].isspace():
            return r
    return 0


def _statement_body(lines, cond_line, limit=6):
    """条件行所辖的语句体 → (文本, 结束行号)，认不出返回 (None, None)。

    本树三种写法都要认：`if(...) {` 同行开块、`if(...)` 换行 `{`、
    `if(!x)` 换行 `continue;`（单条语句、没有花括号），且条件本身可跨行。
    """
    cond_line = _condition_end(lines, cond_line, limit)
    brace = lines[cond_line].find("{")
    if brace != -1:
        end_r, _ = _block_end(lines, cond_line, brace)
        if end_r is None:
            return None, None
        return "\n".join(lines[cond_line:end_r + 1]), end_r
    r = cond_line + 1
    while r < len(lines) and r - cond_line <= limit:
        if not lines[r].strip():
            r += 1
            continue
        b = lines[r].find("{")
        if b != -1:
            end_r, _ = _block_end(lines, r, b)
            if end_r is None:
                return None, None
            return "\n".join(lines[cond_line:end_r + 1]), end_r
        # 单条语句，且它自己也可能跨行（`if(x)` 换行 `sum +=` 换行 `f();`），
        # 所以要续到出现 `;` 为止，否则语句体截断、end_row 落在解引用之前。
        rr = r
        while rr < len(lines) and rr - cond_line <= limit + 4:
            if ";" in lines[rr]:
                break
            rr += 1
        return "\n".join(lines[cond_line:rr + 1]), rr
    return None, None


def _condition_start(lines, j, limit=5):
    """提到本对象的第 j 行往上找辖它的 `if` 行。

    条件是允许跨行的（`if (!a || !b ||` 换行 `!c)`），所以不能要求 `if` 与
    对象名同行 —— 只要求往上几行内出现 `if`，且中途没有语句结束符。
    """
    k = j
    while k >= 0 and j - k <= limit:
        if re.search(r"\bif\b", lines[k]):
            return k
        if k != j and re.search(r"[;{}]", lines[k]):
            return None
        k -= 1
    return None


def _guard_before(lines, index, col, ident, depths=None):
    """支配 lines[index][col] 处解引用的守卫；返回命中的规则名或 None。"""
    if depths is None:
        depths = _brace_depths(lines)
    # R0 同行短路：解引用**位置之前**的本行文本已经测过这个对象。
    # 必须用 col（匹配起点）而不是 find()：`!x || !(x->dT>0)` 里 find 取的是
    # 第一个 x，那段前缀当然不含 x，会把短路保护误判成没守卫。
    if _mentions(lines[index][:col], ident):
        return "R0"

    # R1 外层条件
    if any(_mentions(c, ident) for c in _enclosing_conditions(lines, index)):
        return "R1"

    for j in range(index - 1, _scope_start(lines, index), -1):
        if not _mentions(lines[j], ident):
            continue
        k = _condition_start(lines, j)
        if k is None or k >= index or k <= _scope_start(lines, index):
            continue
        cond = "\n".join(lines[k:j + 1])
        body, body_end = _statement_body(lines, k)
        if body is None:
            continue
        negated = re.search(rf"!\s*{re.escape(ident)}", cond)
        if negated:
            # R2 跳过式：否定条件 + 语句体真的退出，且守卫整体在解引用之前
            if body_end is not None and body_end < index and \
               _dominates(depths, k, body_end, index) and \
               re.search(r"\b(continue|return)\b", body):
                between = "\n".join(lines[k:index])
                if not re.search(rf"{re.escape(ident)}\s*=", between):
                    return "R2"
        else:
            # R3 正向：`if(<对象>)` 且解引用就落在它的语句体里
            if body_end is not None and k <= index <= body_end:
                return "R3"
    return None


class PreintegrationGuardContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = {}
        for path in sorted(SRC.glob("*.cc")) + sorted(INCLUDE.glob("*.h")):
            cls.files[path] = _strip_comments_and_strings(
                path.read_text(errors="replace").splitlines())

    def test_every_dereference_is_dominated_by_a_null_check(self):
        offenders, checked = [], 0
        for path, lines in self.files.items():
            for i, line in enumerate(lines):
                for m in DEREF.finditer(line):
                    checked += 1
                    ident = _identity(m.group(1), m.group(2), m.group(3))
                    if _guard_before(lines, i, m.start(), ident) is None:
                        offenders.append(f"{path.name}:{i + 1}  {line.strip()[:70]}")

        self.assertGreater(checked, 15,
                           f"只扫到 {checked} 处解引用，扫描器多半坏了"
                           f"（本树应有 20+ 处）")
        self.assertEqual(
            offenders, [],
            "有 mpImuPreintegrated 解引用没有被判空支配 —— 空预积分是设计内的"
            "合法态（IMU 空档期建的关键帧就是 NULL），会直接 SIGSEGV：\n  "
            + "\n  ".join(offenders))

    def test_no_print_without_skip_pattern(self):
        """盯 2026-09-23 的原始形态：判空了却继续往下解引用。"""
        offenders = []
        for path, lines in self.files.items():
            for i, line in enumerate(lines):
                if not re.search(rf"\bif\b.*!\s*[\w\)\]]*{MEMBER}", line):
                    continue
                body, _ = _statement_body(lines, i)
                if body is not None and not re.search(r"\b(continue|return)\b", body):
                    offenders.append(f"{path.name}:{i + 1}  {line.strip()[:70]}")
        self.assertEqual(
            offenders, [],
            "发现「判空但不跳过」——这正是 2026-09-23 崩溃的形态"
            "（打印 Not preintegrated measurement 后照样 SetNewBias）：\n  "
            + "\n  ".join(offenders))

    def test_inertial_optimization_edge_loops_guard_both_overloads(self):
        """两个 InertialOptimization 重载的加边循环都必须判空。

        2026-09-23 崩的是 11 参数重载（LocalMapping::InitializeIMU 调它），
        而 5 参数重载连打印都没有、同样没判空——只修前者会剩一颗地雷。
        """
        source = "\n".join(self.files[SRC / "Optimizer.cc"])
        self.assertEqual(
            len(re.findall(r"\nvoid Optimizer::InertialOptimization\s*\(", source)), 3,
            "InertialOptimization 重载数变了；新增重载必须同样判空")

        # 按签名切出两个「加边循环」重载的函数体（第三个只有 Map*,Rwg,scale）
        bodies = {}
        for label, sig in (
            ("11 参数", r"void Optimizer::InertialOptimization\(Map \*pMap, "
                       r"Eigen::Matrix3d &Rwg, double &scale,"),
            ("5 参数", r"void Optimizer::InertialOptimization\(Map \*pMap, "
                      r"Eigen::Vector3d &bg,"),
        ):
            start = re.search(sig, source)
            self.assertIsNotNone(start, f"找不到 {label} 重载的签名")
            bodies[label] = source[start.start():source.find("\nvoid Optimizer::",
                                                             start.end())]

        for label, body in bodies.items():
            self.assertRegex(
                body, r"!\s*pKFi->mpImuPreintegrated\s*\)\s*\{[^}]*continue;",
                f"{label}重载的加边循环里，判空后没有 continue —— "
                f"只打印不跳过等于没判（2026-09-23 崩溃的原始形态）")
            # 判空必须**支配**SetNewBias：守卫块的花括号要闭合在它之前
            guard = re.search(r"!\s*pKFi->mpImuPreintegrated", body)
            bias = body.find("pKFi->mpImuPreintegrated->SetNewBias")
            self.assertNotEqual(bias, -1, f"{label}重载里找不到 SetNewBias")
            self.assertLess(guard.start(), bias,
                            f"{label}重载的判空排在 SetNewBias 之后，等于没判")

    def test_full_inertial_ba_gates_on_preintegration_too(self):
        """FullInertialBA 的条件必须同时判 bImu 与预积分。

        `bImu` 记的是「这个关键帧有 IMU 采样」，不是「建出了预积分」——
        两者可以同时成立与不成立，只看 bImu 会漏。
        """
        source = "\n".join(self.files[SRC / "Optimizer.cc"])
        start = source.find("void Optimizer::FullInertialBA")
        self.assertNotEqual(start, -1, "找不到 FullInertialBA 定义")
        body = source[start:source.find("\nvoid Optimizer::", start + 1)]

        self.assertRegex(
            body, r"pKFi->bImu\s*&&\s*pKFi->mPrevKF->bImu\s*&&\s*"
                  r"pKFi->mpImuPreintegrated",
            "FullInertialBA 只看 bImu 没看 mpImuPreintegrated —— "
            "有 IMU 采样但没建出预积分的关键帧会在这里 SIGSEGV")

    # 本轮 5 处修复的「修复前 / 修复后」对照。反着用：把已修好的文本换回
    # 修复前的样子，扫描器必须报出至少一处 —— 报了才证明它有牙。
    UNDONE = [
        ("Optimizer.cc 11 参数重载：判空退回「只打印不跳过」",
         "Optimizer.cc",
         '                std::cout << "Not preintegrated measurement" << std::endl;\n'
         "                continue;\n            }",
         '                std::cout << "Not preintegrated measurement" << std::endl;\n'
         "            }"),
        ("Optimizer.cc 5 参数重载：摘掉整个判空",
         "Optimizer.cc",
         "            if(!pKFi->mpImuPreintegrated)\n            {\n"
         '                std::cout << "Not preintegrated measurement" << std::endl;\n'
         "                continue;\n            }\n\n"
         "            pKFi->mpImuPreintegrated->SetNewBias(pKFi->mPrevKF->GetImuBias());",
         "            pKFi->mpImuPreintegrated->SetNewBias(pKFi->mPrevKF->GetImuBias());"),
        ("Optimizer.cc FullInertialBA：退回只看 bImu",
         "Optimizer.cc",
         "if(pKFi->bImu && pKFi->mPrevKF->bImu && pKFi->mpImuPreintegrated)",
         "if(pKFi->bImu && pKFi->mPrevKF->bImu)"),
        ("LocalMapping.cc 清理路径：摘掉 DiscardMeasurements 的判空",
         "LocalMapping.cc",
         "                            if(keyFrame->mpImuPreintegrated)\n"
         "                                runDiscardedMeasurements +=\n"
         "                                    keyFrame->mpImuPreintegrated->DiscardMeasurements();",
         "                            runDiscardedMeasurements +=\n"
         "                                keyFrame->mpImuPreintegrated->DiscardMeasurements();"),
        ("Tracking.cc 单目分支：摘掉重新播种的判空",
         "Tracking.cc",
         "        if(pKFcur->mpImuPreintegrated)\n"
         "            mpImuPreintegratedFromLastKF = new IMU::Preintegrated("
         "pKFcur->mpImuPreintegrated->GetUpdatedBias(),pKFcur->mImuCalib);",
         "            mpImuPreintegratedFromLastKF = new IMU::Preintegrated("
         "pKFcur->mpImuPreintegrated->GetUpdatedBias(),pKFcur->mImuCalib);"),
    ]

    def test_scanner_keeps_its_teeth(self):
        """把 5 处修复逐一改回原样，扫描器必须变红。"""
        for label, fname, fixed, broken in self.UNDONE:
            text = (SRC / fname).read_text(errors="replace")
            self.assertIn(fixed, text,
                          f"{label}：锚点没找到，本条已失效，必须按新代码重写锚点")
            lines = _strip_comments_and_strings(
                text.replace(fixed, broken, 1).splitlines())
            depths = _brace_depths(lines)
            offenders = []
            for i, line in enumerate(lines):
                for m in DEREF.finditer(line):
                    ident = _identity(m.group(1), m.group(2), m.group(3))
                    if _guard_before(lines, i, m.start(), ident, depths) is None:
                        offenders.append(f"{fname}:{i + 1}")
            self.assertNotEqual(
                offenders, [],
                f"{label}：摘掉守卫后扫描器一声不吭 —— 这条规则没牙，"
                f"等于给后面的人一张假绿灯")

    def test_map_cleanup_discard_measurements_guards_null(self):
        """地图清理路径的 DiscardMeasurements 调用必须判空（同一族）。"""
        source = "\n".join(self.files[SRC / "LocalMapping.cc"])
        hits = [m.start() for m in re.finditer(r"DiscardMeasurements\(\)", source)]
        self.assertEqual(len(hits), 1,
                         f"DiscardMeasurements 调用点数量变了（{len(hits)} 个）")
        window = source[max(0, hits[0] - 400):hits[0]]
        self.assertRegex(
            window, r"if\s*\(\s*keyFrame->mpImuPreintegrated\s*\)",
            "地图清理里 DiscardMeasurements 没有判空 —— 清理到 IMU 空档期建的"
            "关键帧（预积分为 NULL）就会 SIGSEGV")


if __name__ == "__main__":
    unittest.main()
