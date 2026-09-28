"""训练域 —— 产物发现、GPU 读取,以及将来的训练执行。

**目前不含任何训练执行。** 下面三个模块都只是**只读扫描**:

    gpus.py       本机 GPU(nvidia-smi)
    datasets.py   state/datasets/ 下的可训练数据集
    runs.py       settings.train_root 下的训练产物(checkpoints)

将来接执行时,子进程管理、任务状态、日志解析落在 ``runner.py``;路由仍旧留在
``app/routes/training.py``(与 ``app/ai_annotation.py`` + ``routes/annotations.py``
的分工一致:域逻辑在域包里,HTTP 那层很薄)。

**为什么单独开一个包而不是继续放 ``routes/training.py``**:和前端
``static/js/train/`` 对称,让训练功能一开始就有个像样的家。反例就在旁边 ——
``app/ai_annotation.py`` 长到 180KB 才想起来该拆。新功能先模块化,比事后从
大文件里往外抠容易得多。
"""

from app.training.datasets import DATASETS_DIR, list_datasets
from app.training.gpus import list_gpus
from app.training.projects import list_projects_progress
from app.training.runs import list_runs

__all__ = ["DATASETS_DIR", "list_datasets", "list_gpus", "list_projects_progress", "list_runs"]
