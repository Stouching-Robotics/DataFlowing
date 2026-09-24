"""清洗契约与检查项注册表单测。

覆盖:设备模态与 device_naming 的口径一致性(漂移守卫)、别名映射、
信道推导、严重度合成、检查项筛选。

    pytest tests/test_cleaning_contract.py
    python3 tests/test_cleaning_contract.py     # 无 pytest 也能跑
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.device_naming import DEVICE_LABELS  # noqa: E402
from app.processing.cleaning import contract as C  # noqa: E402
from app.processing.cleaning import checks as K  # noqa: E402


# ── 漂移守卫 ────────────────────────────────────────────────
def test_device_modalities_match_device_naming():
    """两个文件声明的设备模态必须逐字一致。

    这是本套设计里唯一靠"人工保持同步"的地方 —— 所以用测试锁死。
    任何人在 device_naming 加了新设备类型而忘了同步这里，这个测试立刻红。
    """
    assert set(C.DEVICE_MODALITIES) == set(DEVICE_LABELS)


def test_every_channel_of_every_modality_is_declared():
    """MODALITY_CHANNELS 用到的信道都必须在 CHANNELS 里声明。"""
    declared = set(C.CHANNELS)
    for modality in C.DEVICE_MODALITIES:
        assert modality in C.MODALITY_CHANNELS, f"{modality} 没声明信道"
        unknown = set(C.MODALITY_CHANNELS[modality]) - declared
        assert not unknown, f"{modality} 用了未声明的信道: {unknown}"


def test_aliases_map_into_canonical_set():
    """别名表的值必须是合法的设备模态。"""
    for alias, target in C._ALIASES.items():
        assert target in C.DEVICE_MODALITIES, f"{alias} → {target} 不是合法模态"


# ── 别名映射 ────────────────────────────────────────────────
def test_normalize_accepts_canonical_and_alias():
    # canonical 值原样返回
    assert C.normalize_device_modality("mono_rgb") == "mono_rgb"
    assert C.normalize_device_modality("gripper_device") == "gripper_device"
    # api/projects 与处理模块 slug 用的 camera 后缀写法
    assert C.normalize_device_modality("mono_camera") == "mono_rgb"
    assert C.normalize_device_modality("stereo_camera") == "stereo_rgb"
    # 历史节点类型
    assert C.normalize_device_modality("fisheye_camera") == "mono_rgb"


def test_normalize_rejects_unknown_without_raising():
    """认不出来要返回 None 而不是抛异常 —— 调用方据此跳过。"""
    assert C.normalize_device_modality("") is None
    assert C.normalize_device_modality(None) is None
    assert C.normalize_device_modality("some_future_sensor") is None


def test_normalize_accepts_whitespace():
    assert C.normalize_device_modality("  mono_rgb  ") == "mono_rgb"


# ── 信道推导 ────────────────────────────────────────────────
def test_channels_for_unions_and_dedupes():
    channels = C.channels_for(["gripper_device", "mono_rgb"])
    assert channels[0] == "rgb"          # 保序:gripper_device 先出现
    assert "time" in channels
    assert len(channels) == len(set(channels)), "有重复信道"


def test_channels_for_unknown_modality_is_empty():
    assert C.channels_for(["not_a_modality"]) == ()
    assert C.channels_for([]) == ()


# ── 严重度 ──────────────────────────────────────────────────
def test_worst_picks_most_severe():
    assert C.worst([C.PASS, C.WARN]) == C.WARN
    assert C.worst([C.WARN, C.FAIL]) == C.FAIL
    assert C.worst([C.FAIL, C.ERROR]) == C.ERROR
    assert C.worst([C.ERROR, C.PENDING, C.WARN]) == C.ERROR


def test_worst_of_empty_is_pass():
    assert C.worst([]) == C.PASS


def test_worst_ignores_unknown_status_names():
    """未知状态名按"最轻"处理,不崩溃 —— 新旧报告混读时不炸。"""
    assert C.worst([C.PASS, "some_new_status"]) == C.PASS


def test_is_blocking_only_for_fail_and_error():
    assert C.is_blocking(C.FAIL) is True
    assert C.is_blocking(C.ERROR) is True
    assert C.is_blocking(C.WARN) is False
    assert C.is_blocking(C.PENDING) is False
    assert C.is_blocking(C.PASS) is False


# ── 检查项筛选 ──────────────────────────────────────────────
class _FakeVideoCheck(K.CleaningCheck):
    slug = "_test.video"
    label = "fake video"
    requires_channels = ("rgb",)
    # 多张卡片共享 —— video.* 就是这样，只要命中其一就跑
    device_cards = ("mono_rgb", "stereo_rgb", "rgbd_camera", "gripper_device")


class _FakeUmiCheck(K.CleaningCheck):
    slug = "_test.umi"
    label = "fake umi"
    requires_channels = ("slam",)
    requires_modality = ("gripper_device",)
    device_cards = ("gripper_device",)


class _FakeCrossCheck(K.CleaningCheck):
    slug = "_test.cross"
    label = "fake cross"
    cross_modal = True


def _with_checks(*classes, fn):
    """临时注册一批检查项,跑完清理 —— 不污染全局注册表。"""
    registered = []
    try:
        for cls in classes:
            K._registry[cls.slug] = cls()
            registered.append(cls.slug)
        return fn()
    finally:
        for slug in registered:
            K._registry.pop(slug, None)


def test_applicable_filters_by_channel():
    def run():
        got = {c.slug for c in K.applicable_checks({"rgb"}, {"mono_rgb"})}
        assert "_test.video" in got
        assert "_test.umi" not in got          # 缺 slam 信道
    _with_checks(_FakeVideoCheck, _FakeUmiCheck, fn=run)


def test_applicable_filters_by_modality():
    def run():
        # 有 slam 信道但设备不是 gripper_device → umi 检查不跑
        got = {c.slug for c in K.applicable_checks({"slam"}, {"mono_rgb"})}
        assert "_test.umi" not in got
        got = {c.slug for c in K.applicable_checks({"slam"}, {"gripper_device"})}
        assert "_test.umi" in got
    _with_checks(_FakeUmiCheck, fn=run)


def test_applicable_filters_by_device_cards():
    """设备卡片闸门 —— 光按信道筛不够。

    真实踩过的坑：手套项目（rgbd_camera）的批次里同样有一列 ``action``
    （采集端写的全零占位），于是 ``umi.action_present`` 满足
    ``requires_channels=("action",)`` 就跑了起来，给手套项目报了个
    "action 全零"的失败 —— 而对方根本不训练动作。

    声明了 device_cards 的检查项，必须只在该设备上跑。
    """
    def run():
        channels = {"action", "slam"}   # 信道全够，只剩设备这一关
        # 设备是手套相机 → UMI 检查不跑，尽管信道满足了
        got = {c.slug for c in K.applicable_checks(channels, {"rgbd_camera"})}
        assert "_test.umi" not in got
        # 设备是 UMI 夹爪 → 跑
        got = {c.slug for c in K.applicable_checks(channels, {"gripper_device"})}
        assert "_test.umi" in got
    _with_checks(_FakeUmiCheck, fn=run)


def test_device_cards_gate_is_skipped_for_shared_checks():
    """声明了多张卡片的检查项，只要命中其一就跑（video.* 就是这种）。"""
    def run():
        got = {c.slug for c in K.applicable_checks({"rgb"}, {"stereo_rgb"})}
        assert "_test.video" in got
    _with_checks(_FakeVideoCheck, fn=run)


def test_cross_modal_requires_two_modalities():
    def run():
        # 单模态 → 跨模态检查不跑
        got = {c.slug for c in K.applicable_checks(set(), {"mono_rgb"})}
        assert "_test.cross" not in got
        # 显式声明跨模态成立 → 跑
        got = {c.slug for c in K.applicable_checks(
            set(), {"mono_rgb", "glove_sensor"}, cross_modal_active=True)}
        assert "_test.cross" in got
    _with_checks(_FakeCrossCheck, fn=run)


def test_applicable_respects_enabled_and_disabled():
    def run():
        got = {c.slug for c in K.applicable_checks(
            {"rgb"}, {"mono_rgb"}, disabled={"_test.video"})}
        assert "_test.video" not in got
        got = {c.slug for c in K.applicable_checks(
            {"rgb"}, {"mono_rgb"}, enabled={"_test.video"})}
        assert got == {"_test.video"}
    _with_checks(_FakeVideoCheck, fn=run)


def test_all_checks_is_sorted_by_slug():
    """排序稳定 —— ruleset_revision 是对它取哈希,顺序变了指纹就变。"""
    slugs = [c.slug for c in K.all_checks()]
    assert slugs == sorted(slugs)


def test_register_rejects_duplicate_slug():
    class First(K.CleaningCheck):
        slug = "_test.dupe"

    class Second(K.CleaningCheck):
        slug = "_test.dupe"

    try:
        K.register_check(First)
        try:
            K.register_check(Second)
        except ValueError as exc:
            assert "_test.dupe" in str(exc)
        else:
            raise AssertionError("重复 slug 应当抛 ValueError")
    finally:
        K._registry.pop("_test.dupe", None)


def test_register_rejects_empty_slug():
    class NoSlug(K.CleaningCheck):
        slug = ""

    try:
        K.register_check(NoSlug)
    except ValueError:
        pass
    else:
        raise AssertionError("空 slug 应当抛 ValueError")


# ── 设备卡片分组（设置面板的 tab）────────────────────────────
def test_every_check_declares_device_cards():
    """除跨设备检查外，每项都必须声明属于哪些设备卡片。

    否则它在设置面板里【无处可去】—— 用户看不到、也改不了它的阈值。
    跨设备检查（cross_modal=True）约定不绑单卡片，只在"跨设备" tab 出现。
    """
    for check in K.all_checks():
        if check.cross_modal:
            continue
        assert check.device_cards, f"{check.slug} 没有声明 device_cards"


def test_device_cards_are_known_modalities():
    """device_cards 的取值必须来自 contract 的设备模态表 —— 防止拼错字符串。

    拼错不会报错，只会让检查项在面板上凭空消失，很难发现。
    """
    known = set(C.DEVICE_MODALITIES)
    for check in K.all_checks():
        for card in check.device_cards:
            assert card in known, f"{check.slug} 声明了未知设备卡片 {card!r}"


def test_checks_for_device_cards_only_returns_requested():
    """未连接的设备不该出现在分组结果里。"""
    grouped = K.checks_for_device_cards(["gripper_device"])
    assert set(grouped) == {"gripper_device"}
    assert all(c.device_cards.count("gripper_device") for c in grouped["gripper_device"])


def test_checks_for_device_cards_places_multi_card_checks():
    """一个检查项可以同时属于多张卡片（video.* 对每种带摄像头的设备都成立）。"""
    grouped = K.checks_for_device_cards(["rgbd_camera", "gripper_device"])
    in_a = {c.slug for c in grouped["rgbd_camera"]}
    in_b = {c.slug for c in grouped["gripper_device"]}
    assert "video.frame_drop" in in_a and "video.frame_drop" in in_b
    # UMI 专有的只出现在 gripper_device 下
    assert "umi.slam_continuity" in in_b and "umi.slam_continuity" not in in_a


def test_video_checks_cover_gripper_device():
    """UMI 夹爪自带鱼眼相机，视频检查对它同样成立。

    历史坑：video.* 早期只声明了四种摄像机模态，漏了 gripper_device ——
    结果 UMI 项目的设置面板里视频检查整组消失。
    """
    grouped = K.checks_for_device_cards(["gripper_device"])
    slugs = {c.slug for c in grouped["gripper_device"]}
    assert {"video.frame_drop", "video.black_screen",
            "video.freeze", "video.decode_error"} <= slugs


if __name__ == "__main__":
    _failures = 0
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        import inspect
        try:
            if "tmp_path" in inspect.signature(_fn).parameters:
                import tempfile
                with tempfile.TemporaryDirectory() as _tmp:
                    _fn(Path(_tmp))
            else:
                _fn()
            print(f"PASS {_name}")
        except Exception as _exc:  # noqa: BLE001
            _failures += 1
            print(f"FAIL {_name}: {_exc}")
    raise SystemExit(1 if _failures else 0)
