#!/usr/bin/env python3
"""静态安全契约：Frame::mpLastKeyFrame 的初始化与时间链链头。

2026-09-17 的 SIGSEGV（`fault_addr=0x283`，`UpdateLocalKeyFrames()+0x424`，
`cmp %rax,0x30(%rbx)` 且 `rbx=0x253`）根因是 `Frame::mpLastKeyFrame` 从没被
图像构造函数初始化过：`Frame.h` 里它没有初值，`Frame.cc` 里只有拷贝构造提过
它，而 `GrabImageStereo` 是**拷贝赋值一个临时对象**，于是临时对象里的不确定值
整份进了 `mCurrentFrame`。`PreintegrateIMU()` 正常路径会覆盖它（成功分支），
但它的两条提前返回都从那一行**前面**走掉，残留值就被
`UpdateLocalKeyFrames()` 当成时间链链头走了 20 步。

这个契约测试盯着三件事，任何一件被改动都会红：
  1. 每个非拷贝构造函数都必须显式初始化 mpLastKeyFrame；
  2. PreintegrateIMU 的每条「跳过」提前返回都必须先把链头写正；
  3. KeyFrameCulling 的惯性分支必须两侧预积分都判空。

原生侧的行为验证在 online/tests/native/frame_lastkf_init_test.cc（ctest 名
frame-lastkf-init-safety，用 0xAB 毒化内存构造，直接看构造函数漏没漏）。
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRAME_CC = ROOT / "ORB-SLAM/src/Frame.cc"
TRACKING_CC = ROOT / "ORB-SLAM/src/Tracking.cc"
LOCAL_MAPPING_CC = ROOT / "ORB-SLAM/src/LocalMapping.cc"


def _ctor_init_lists(source):
    """返回 {构造函数签名: 初始化列表文本}，只取定义（Frame::Frame 开头）。"""
    blocks = {}
    # 参数表里没有内层括号（定义里不重复默认实参），所以匹配到第一个 ) 为止；
    # 用 [^\n]* 会贪婪吃到行尾的 mbHasVelocity(false)，把初始化列表整个吞掉。
    for match in re.finditer(r"^Frame::Frame\(([^)\n]*)\)", source, re.M):
        start = match.end()
        brace = source.find("{", start)
        if brace == -1:
            continue
        signature = "Frame(" + " ".join(match.group(1).split()) + ")"
        blocks[signature] = source[start:brace]
    return blocks


class FrameLastKeyFrameContractTest(unittest.TestCase):
    def test_every_image_constructor_initialises_last_keyframe(self):
        ctors = _ctor_init_lists(FRAME_CC.read_text())

        # 1 默认 + 1 拷贝 + 4 图像（双目 / 双目+Tlr / RGBD / 单目）—— 数量本身
        # 就是提醒：新增构造函数必须一并处理。
        self.assertEqual(
            len(ctors), 6,
            f"Frame 构造函数数量变了（{len(ctors)} 个，预期 6 个），新增的必须"
            f"同样初始化 mpLastKeyFrame: {sorted(ctors)}")

        copy_ctors = [name for name in ctors if "const Frame &frame" in name]
        self.assertEqual(len(copy_ctors), 1, "找不到拷贝构造函数")
        for name, init_list in ctors.items():
            if name in copy_ctors:
                # 拷贝构造必须**继承**（而不是重置）链头：mLastFrame 靠它记住
                # 「这一帧跟着哪个关键帧」。
                self.assertIn(
                    "mpLastKeyFrame(frame.mpLastKeyFrame)", init_list,
                    "拷贝构造不再继承 mpLastKeyFrame —— mLastFrame 会丢失链头")
                continue
            self.assertIn(
                "mpLastKeyFrame(", init_list,
                f"{name} 没有初始化 mpLastKeyFrame。它是裸指针，不写就是不确定"
                f"值；GrabImageStereo 拷贝赋值临时对象时会把它整份带进"
                f" mCurrentFrame，UpdateLocalKeyFrames() 随后拿它当时间链链头")

    def test_skip_returns_write_the_chain_head(self):
        source = TRACKING_CC.read_text()
        start = source.find("bool Tracking::PreintegrateIMU()")
        self.assertNotEqual(start, -1, "找不到 Tracking::PreintegrateIMU 定义")
        end = source.find("\n}\n", start)
        body = source[start:end]

        # 跳过路径的形态固定为 setIntegrated(); return false;。目前 4 条：
        #   ① 没有上一帧（"non prev frame"）
        #   ② IMU 队列整个是空的（"Not IMU data in mlQueueImuData!!"）
        #   ③ n <= 0（"Insufficient IMU measurements"，2026-09-17 崩溃就是这条）
        #   ④ 缺 KF 累加器（"[IMU_RECOVERY] missing keyframe accumulator"）
        # 数量对不上说明加了新的提前返回 —— 新的那条默认也会漏掉链头赋值，
        # 这正是崩溃的成因，所以这里要拦下来。
        skips = [m.start() for m in
                 re.finditer(r"mCurrentFrame\.setIntegrated\(\);\s*\n\s*"
                             r"return false;", body)]
        self.assertEqual(
            len(skips), 4,
            f"PreintegrateIMU 的提前返回数量变了（{len(skips)} 条，预期 4 条）。"
            f"每一条都必须先把 mCurrentFrame.mpLastKeyFrame 写正，否则构造函数"
            f"留下的不确定值会原样进入 UpdateLocalKeyFrames()")

        for pos in skips:
            window = body[max(0, pos - 600):pos]
            self.assertIn(
                "mCurrentFrame.mpLastKeyFrame = mpLastKeyFrame;", window,
                "PreintegrateIMU 的这条提前返回没有写正链头 —— 这正是 2026-09-17 "
                "崩溃的漏点（跳过赋值 → 残留值当链头 → fault_addr=0x283）")

    def test_success_path_still_assigns_chain_head(self):
        source = TRACKING_CC.read_text()
        self.assertIn("mCurrentFrame.mpLastKeyFrame = mpLastKeyFrame;", source,
                      "成功路径的链头赋值被删了")

        # 链头必须在时间链遍历处被当成可能为空的值对待（循环开头有 break），
        # 但不能只靠这个 break：残留值非空时它形同虚设，所以才要上面两条。
        local_kf = source.find("void Tracking::UpdateLocalKeyFrames()")
        self.assertNotEqual(local_kf, -1, "找不到 UpdateLocalKeyFrames 定义")
        walk = source[local_kf:source.find("\n}\n", local_kf)]
        self.assertIn("KeyFrame* tempKeyFrame = mCurrentFrame.mpLastKeyFrame;", walk)
        self.assertIn("if (!tempKeyFrame)", walk,
                      "时间链遍历开头的判空被删了")

    def test_kfculling_guards_both_preintegrals(self):
        source = LOCAL_MAPPING_CC.read_text()
        start = source.find("void LocalMapping::KeyFrameCulling()")
        self.assertNotEqual(start, -1, "找不到 KeyFrameCulling 定义")
        body = source[start:source.find("\n}\n", start)]

        # MergePrevious 收的是本关键帧的累加器、调的是下一个关键帧的累加器：
        # 两侧都可能为空（IMU 空档期建的关键帧按设计就是 NULL），漏判任一侧
        # 都是 2026-09-16 那次 `fault_addr=0x8b8`（this==NULL）。
        guard_marker = ("pKF->mNextKF->mpImuPreintegrated && "
                        "pKF->mpImuPreintegrated")
        self.assertIn(guard_marker, body,
                      "KeyFrameCulling 的惯性分支又少判了预积分空指针")
        self.assertEqual(body.count("MergePrevious(pKF->mpImuPreintegrated)"), 2,
                         "MergePrevious 调用点数量变了，判空要跟着覆盖")

        # 判空必须**支配**两处 MergePrevious：花括号配平找出它守的那个块，
        # 两处调用都得落在块里（只看 `if(` 会误配到内层的 `if((bInitImu...`）。
        guard_at = body.find(guard_marker)
        block_start = body.find("{", guard_at)
        self.assertNotEqual(block_start, -1, "判空后面没有块")
        depth = 0
        for index in range(block_start, len(body)):
            if body[index] == "{":
                depth += 1
            elif body[index] == "}":
                depth -= 1
                if depth == 0:
                    block_end = index
                    break
        else:
            self.fail("判空块的括号配不平")
        for match in re.finditer(r"mpImuPreintegrated->MergePrevious", body):
            self.assertTrue(
                block_start < match.start() < block_end,
                "有 MergePrevious 落在预积分判空块之外 —— 空预积分状态下会 "
                "SIGSEGV（this==NULL，fault_addr=0x8b8）")


if __name__ == "__main__":
    unittest.main()
