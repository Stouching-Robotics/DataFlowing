"""Application configuration via environment variables."""

from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database. In local mode this normally points to the server through an
    # SSH tunnel (for example 127.0.0.1:15432).
    DATABASE_URL: str = "postgresql+asyncpg://odoo:myodoo@172.19.0.2:5432/data_acq"
    DB_CONNECT_TIMEOUT: float = 5.0
    DB_COMMAND_TIMEOUT: float = 10.0
    DB_POOL_TIMEOUT: float = 5.0

    # Local cache/staging directory. In SFTP mode the authoritative copy is
    # SFTP_ROOT on the storage server.
    STORAGE_DIR: str = "data"
    STORAGE_BACKEND: str = "local"  # local | sftp
    SFTP_HOST: str = "192.168.110.41"
    SFTP_PORT: int = 22
    SFTP_USERNAME: str = "Stouch"
    SFTP_PASSWORD: str = ""
    SFTP_KEY_FILE: str = ""
    SFTP_ROOT: str = "/vol1/docker/odoo-dev/Data Acquisition/data"
    SFTP_KNOWN_HOSTS: str = ""
    SFTP_STRICT_HOST_KEY: bool = False
    SFTP_CONNECT_TIMEOUT: int = 15

    # Auth
    API_KEY: str = "change-me"
    WORKER_API_KEY: str = ""
    DEFAULT_WORKFLOW_ID: str = ""
    # A cold NAS input-package build can take longer than the normal polling
    # request. Active jobs still send heartbeats during processing.
    WORKER_LEASE_SECONDS: int = 300

    # Upload limits
    MAX_UPLOAD_SIZE: int = 2 * 1024 * 1024 * 1024  # 2GB
    # Uploads are first spooled on the server's local disk.  The authoritative
    # dataset may live on the SSHFS/NAS mount, but receiving a large archive
    # must not make the API wait on remote filesystem writes.
    UPLOAD_STAGING_DIR: str = ""
    ALLOWED_VIDEO_EXTENSIONS: set[str] = {"mp4", "mkv", "mov", "avi", "m4v"}
    ALLOWED_IMAGE_EXTENSIONS: set[str] = {"jpg", "jpeg", "png", "bmp"}

    # Local server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    PUBLIC_BASE_URL: str = "http://192.168.110.41:2586"
    LOG_LEVEL: str = "info"

    # Cleaning
    CLEANING_ENABLED: bool = False

    # Export
    EXPORT_SHARD_SIZE: int = 100000
    VIDEO_SHARD_FRAMES: int = 5000

    # Training. Checkpoints are produced by the official ``lerobot-train`` CLI
    # (conda env ``lerobot052_base_piper``). Empty = use the default below.
    TRAIN_OUTPUT_ROOT: str = ""
    # ``lerobot-train`` 可执行文件。**指向 conda 环境里的那个,不要用
    # ``conda run``** —— conda run 会把子进程输出攒起来,进度条就流不出来,
    # 页面上会显示成一个迟迟不动的黑盒。
    TRAIN_CLI: str = ""
    # 训练成功后每个 run 保留几个 checkpoint。1 = 只留最后一个(默认),
    # 0 = 全留。一个 checkpoint 约 590MB,中间态只对续训有用。
    TRAIN_KEEP_CHECKPOINTS: int = 1

    @property
    def train_cli(self) -> str:
        """``lerobot-train`` 的可执行路径。

        默认写死这个 conda 环境是**有意的**:训练依赖 LeRobot 0.6.2 + Piper
        的那套环境(见 README 的部署说明),换一个环境大概率跑不起来,
        与其猜不如给一个确定能用的默认值,要换再用环境变量覆盖。
        """
        raw = str(self.TRAIN_CLI or "").strip()
        if raw:
            return str(Path(raw).expanduser())
        return str(Path.home() / "anaconda3" / "envs" / "lerobot052_base_piper"
                   / "bin" / "lerobot-train")

    @property
    def train_root(self) -> Path:
        """Trained-model directory — **read-only**, never created here.

        Defaults to ``<repo>/文件测试/umi_act_model``, i.e. beside ``processor/``.
        The directory is git-ignored and may legitimately be absent (fresh
        clone, or training never run on this host): callers must treat a
        missing path as "no runs" rather than creating it.  Creating it would
        make an empty training dir appear out of nowhere on every boot.
        """
        raw = str(self.TRAIN_OUTPUT_ROOT or "").strip()
        if raw:
            return Path(raw).expanduser()
        return (Path(__file__).resolve().parent.parent.parent
                / "文件测试" / "umi_act_model")

    model_config = {
        "env_prefix": "",
        "case_sensitive": False,
        "env_file": ".env",
        "extra": "ignore",
    }

    @property
    def storage_root(self) -> Path:
        """Absolute path to storage directory."""
        p = Path(self.STORAGE_DIR)
        if not p.is_absolute():
            p = Path(__file__).parent.parent / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def temp_root(self) -> Path:
        """Project-local temporary workspace; never falls back to the OS temp dir."""
        p = self.storage_root / "tmp"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def upload_staging_root(self) -> Path:
        """Local durable spool for incoming archives.

        ``storage_root`` can be an SSHFS mount.  Keep the receive spool on the
        local machine so the upload response is independent of NAS latency.
        The queue persists its small job records in the authoritative state
        directory and resumes jobs whose spool file still exists.
        """
        raw = str(self.UPLOAD_STAGING_DIR or "").strip()
        p = Path(raw).expanduser() if raw else Path.home() / ".cache" / "egodata-ingest"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()

# Init subdirectories
# exports/cache 已弃用(导出改到 state/exports,缓存为内存级),不再自动创建
for sub in ["videos", "sessions", "tmp"]:
    (settings.storage_root / sub).mkdir(parents=True, exist_ok=True)
