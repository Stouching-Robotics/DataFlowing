"""``app/training/runner.py`` 的纯逻辑测试。

重点是 ``build_command``:它是唯一"一错就烧几小时 GPU"的地方。**两个已经
实测确认过的坑**写成了回归断言 —— 见下面两个 test_never_emits_* 。
"""

from pathlib import Path

import pytest

from app.config import settings
from app.training.runner import (
    _parse_progress,
    build_command,
    prune_checkpoints,
    safe_job_name,
)


def _spec(**overrides) -> dict:
    """``validate_spec`` 的返回形状 —— build_command 只认这个。"""
    spec = {
        "job_name": "act",
        "dataset": {"name": "umi_act_v1", "path": "/nas/state/datasets/umi_act_v1",
                    "total_episodes": 52},
        "gpus": [0],
        "params": {
            "steps": 20000, "batch_size": 16, "seed": 1000, "num_workers": 4,
            "log_freq": 100, "save_freq": 5000, "save_checkpoint": True,
            "eval_steps": 0,
        },
        "resume": None,
        "output_dir": "/nas/training/act",
    }
    spec.update(overrides)
    return spec


def _args(cmd: list[str]) -> dict[str, str]:
    """把 ``--k=v`` 拆成 dict,方便按参数名断言(顺序不作要求)。"""
    out: dict[str, str] = {}
    for item in cmd[1:]:
        key, _, value = item.partition("=")
        out[key.lstrip("-")] = value
    return out


def test_command_carries_dataset_output_and_every_hyperparameter():
    args = _args(build_command(_spec()))

    assert args["dataset.root"] == "/nas/state/datasets/umi_act_v1"
    assert args["output_dir"] == "/nas/training/act"
    assert args["job_name"] == "act"
    assert args["policy.type"] == "act"
    assert args["steps"] == "20000"
    assert args["batch_size"] == "16"
    assert args["seed"] == "1000"
    assert args["num_workers"] == "4"
    assert args["log_freq"] == "100"
    assert args["save_freq"] == "5000"
    assert args["save_checkpoint"] == "true"


def test_uses_the_configured_conda_cli_not_conda_run():
    cmd = build_command(_spec())

    assert cmd[0] == settings.train_cli
    assert cmd[0].endswith("lerobot-train")
    # ``conda run`` 会缓冲子进程输出,进度条就流不到页面上了。
    assert "conda" not in Path(cmd[0]).name


def test_save_checkpoint_false_is_passed_as_lowercase_flag_string():
    spec = _spec(params={**_spec()["params"], "save_checkpoint": False})
    assert _args(build_command(spec))["save_checkpoint"] == "false"


def test_eval_is_off_by_default_and_needs_both_flags_when_on():
    """``--eval_steps`` 单给不生效 —— ``--help``: "Requires eval_split > 0"。"""
    off = _args(build_command(_spec()))
    assert "eval_steps" not in off
    assert "dataset.eval_split" not in off

    on_spec = _spec(params={**_spec()["params"], "eval_steps": 1000})
    on = _args(build_command(on_spec))
    assert on["eval_steps"] == "1000"
    assert float(on["dataset.eval_split"]) > 0


def test_resume_uses_config_path_not_policy_path():
    """续训 = ``--resume=true --config_path=<ckpt>/pretrained_model/train_config.json``。

    ★ 回归:``app/version.py`` 里"续训需要 --policy.path="那句是旧命令生成器
    留下的**错误**提示 —— 这个 LeRobot 版本 ``policy.*`` 下只有 ``--policy.type``。
    """
    spec = _spec(resume={
        "run": "umi_act_model", "step": 20000,
        "config_path": "/nas/training/umi_act_model/checkpoints/020000"
                       "/pretrained_model/train_config.json",
    })
    args = _args(build_command(spec))

    assert args["resume"] == "true"
    assert args["config_path"].endswith("020000/pretrained_model/train_config.json")
    assert "policy.path" not in args


@pytest.mark.parametrize("banned", ["eval_freq", "policy.path"])
def test_never_emits_flags_that_this_lerobot_version_does_not_have(banned):
    """★ 回归:这两个参数在 ``lerobot052_base_piper`` 的 ``lerobot-train --help``
    里**不存在**。多给一个不认识的 flag,draccus 会直接报错退出 —— 训练起不来,
    而且报错发生在子进程里,页面上只看到一条失败。

    ``eval_freq`` 曾经是表单字段(默认 1000),``policy.path`` 曾经写在注释里;
    两处都已实测证伪。
    """
    cmd = build_command(_spec(resume={
        "run": "r", "step": 5000, "config_path": "/x/train_config.json",
    }))
    assert not any(item.startswith(f"--{banned}=") for item in cmd)


def test_dataset_repo_id_is_always_passed():
    """★ 回归(真跑才发现的坑 5)。

    ``--dataset.repo_id`` 是 ``DatasetConfig`` 的必填字段,哪怕数据集就在本地。
    不给会报 ``Missing required field(s) `dataset` for TrainPipelineConfig`` ——
    注意报的是整个 ``dataset`` 字段缺失,不是"repo_id 缺失",照着字面容易查错方向。
    """
    args = _args(build_command(_spec()))

    assert args["dataset.repo_id"] == "local/umi_act_v1"
    assert args["dataset.root"]  # 单独给 root 不够,两个都要在


def test_push_to_hub_is_explicitly_disabled():
    """★ 回归(真跑才发现的坑 6)。

    ``policy.push_to_hub`` 默认为 true,一旦为 true 就要求顶层 ``repo_id``
    (要从 Hub 推模型),报 ``'repo_id' argument missing. Please specify it to
    push the model to the hub.``

    真实那次训练的 train_config 里**没有顶层 repo_id 键**,只有
    ``policy.push_to_hub: false`` —— 所以正确解法是显式关掉,不是补一个 repo_id。
    """
    args = _args(build_command(_spec()))

    assert args["policy.push_to_hub"] == "false"


def test_safe_job_name_strips_path_separators_and_keeps_it_bounded():
    assert safe_job_name("act_2024-v1.2") == "act_2024-v1.2"
    assert safe_job_name("../../etc/passwd") == "etc_passwd"
    assert safe_job_name("手套 实验/1") == "1"          # 中文与空格都是不合法字符
    assert safe_job_name("") == ""
    assert len(safe_job_name("x" * 200)) <= 64


def test_progress_parser_reads_tqdm_lines_and_ignores_other_fractions():
    log = "Training:  12%|█▏        | 2400/20000 [03:21<24:00, 3.2it/s]\n"
    assert _parse_progress(log, steps=20000) == pytest.approx(0.12)


def test_progress_parser_returns_none_when_it_cannot_be_sure():
    """推不出来就返回 None —— 编一个 0% 会让人以为训练卡住了(见 gpus.py 的态度)。"""
    assert _parse_progress("still importing torch...\n", steps=20000) is None
    # 分母对不上就不是训练进度(可能是别的 x/y,比如保存进度)。
    assert _parse_progress("Saving: 1/4 files\n", steps=20000) is None
    assert _parse_progress("Training: 1/4 [00:01]\n", steps=20000) is None


def test_prune_keeps_only_the_newest_checkpoint_and_repairs_last_symlink(tmp_path):
    run = tmp_path / "act"
    ckpt = run / "checkpoints"
    for step in ("005000", "010000", "015000", "020000"):
        (ckpt / step).mkdir(parents=True)
        (ckpt / step / "model.safetensors").write_bytes(b"x")
    (ckpt / "last").symlink_to("005000")          # 故意指向将被删掉的那个

    removed = prune_checkpoints(run, keep=1)

    assert sorted(removed) == ["005000", "010000", "015000"]
    # 注意排除符号链接 —— ``last`` 指向一个目录,``is_dir()`` 会跟随链接把它也算进来。
    assert [p.name for p in ckpt.iterdir()
            if p.is_dir() and not p.is_symlink()] == ["020000"]
    assert (ckpt / "last").is_symlink()
    assert (ckpt / "last").resolve() == (ckpt / "020000").resolve()


def test_prune_with_keep_zero_is_a_noop(tmp_path):
    ckpt = tmp_path / "act" / "checkpoints"
    for step in ("005000", "020000"):
        (ckpt / step).mkdir(parents=True)

    assert prune_checkpoints(tmp_path / "act", keep=0) == []
    assert sorted(p.name for p in ckpt.iterdir()) == ["005000", "020000"]


def test_prune_never_touches_non_numeric_directories(tmp_path):
    """``last/`` 之类的非数字目录要跳过 —— 删它会连带出问题。"""
    ckpt = tmp_path / "act" / "checkpoints"
    (ckpt / "005000").mkdir(parents=True)
    (ckpt / "020000").mkdir(parents=True)
    (ckpt / "backup").mkdir()

    assert prune_checkpoints(tmp_path / "act", keep=1) == ["005000"]
    assert (ckpt / "backup").is_dir()
