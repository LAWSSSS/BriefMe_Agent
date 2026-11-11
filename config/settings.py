"""项目配置。

所有账号、密码、Token 均通过环境变量或本地 .env 文件注入。
仓库只提供 .env.example，禁止提交真实凭据。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """加载简洁的 KEY=VALUE 格式 .env 文件，不覆盖已有环境变量。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


@dataclass
class VisionConfig:
    """永锋视觉系统配置（打包带项目）。"""

    base_url: str = _env(
        "YONGFENG_PACKING_BASE_URL",
        "http://vision.lg.china-yongfeng.com/packing-tape",
    )
    username: str = _env("YONGFENG_PACKING_USERNAME")
    password: str = _env("YONGFENG_PACKING_PASSWORD")

    # F12 Headers 确认的完整路径（base_url 已包含 /packing-tape）
    login_endpoint: str = "/api/record/doLogin"
    history_endpoint: str = "/api/monitor/query-condition"
    statistic_endpoint: str = "/api/monitor/statistic"


@dataclass
class ScrapConfig:
    """镔鑫钢铁睿视废钢智能质检系统配置。"""

    base_url: str = _env("BINXIN_BASE_URL", "http://172.31.1.102:8081")
    employee_id: str = _env("BINXIN_EMPLOYEE_ID")
    password: str = _env("BINXIN_PASSWORD")

    login_endpoint: str = "/fcs/auth/login"
    list_endpoint: str = "/fcs/intelligence/intelliTaskInfo/page"
    detail_endpoint: str = "/fcs/intelligence/intelliTaskInfo/getCheckDetail"

    # 2=杂摸, 4=中废（字典见 agent/scrap/dict.py）
    exclude_steel_types: tuple = (2, 4)

    target_accuracy: float = 0.95
    target_avg_error_rate: float = 0.10
    target_weight_diff_kg: float = 100.0
    target_weight_ratio_lower: float = 0.5
    target_weight_ratio_upper: float = 1.5


@dataclass
class ShenglongConfig:
    """盛隆钢铁睿视废钢智能质检系统配置。"""

    base_url: str = _env("SHENGLONG_BASE_URL", "http://172.16.16.101:3000")
    employee_id: str = _env("SHENGLONG_EMPLOYEE_ID")
    password: str = _env("SHENGLONG_PASSWORD")

    login_endpoint: str = "/api/auth/login"
    list_endpoint: str = "/api/intelligence/intelliTaskInfo/page"
    detail_endpoint: str = "/api/intelligence/intelliTaskInfo/getCheckDetail"
    cookie_name: str = "scrape-steel-token"

    target_recognition_rate: float = 0.92
    target_deduction_compliance_rate: float = 0.90
    deduction_ratio_lower: float = 0.5
    deduction_ratio_upper: float = 1.5
    deduction_error_tolerance_ton: float = 0.151

    remote_host: str = _env("SHENGLONG_SCP_HOST", "10.180.34.16")
    remote_port: int = int(_env("SHENGLONG_SCP_PORT", "22"))
    remote_user: str = _env("SHENGLONG_SCP_USER", "cisdi")
    remote_image_root: str = _env(
        "SHENGLONG_SCP_ROOT",
        "/mnt/data01/embedded/projects/wangyutai/sl_feigang/test_images_full_car",
    )
    remote_scp_timeout_sec: int = 14400
    remote_scp_stall_sec: int = 600


@dataclass
class YongfengScrapConfig(ShenglongConfig):
    """永锋废钢图片下载配置，与盛隆配置完全分离。"""

    base_url: str = _env(
        "YONGFENG_SCRAP_BASE_URL",
        "http://vision.lg.china-yongfeng.com/srape-steel",
    )
    employee_id: str = _env("YONGFENG_SCRAP_EMPLOYEE_ID")
    password: str = _env("YONGFENG_SCRAP_PASSWORD")
    cookie_name: str = "satoken"

    remote_host: str = _env("YONGFENG_SCP_HOST", "10.233.224.206")
    remote_port: int = int(_env("YONGFENG_SCP_PORT", "22"))
    remote_user: str = _env("YONGFENG_SCP_USER", "cisdi")
    remote_image_root: str = _env(
        "YONGFENG_SCP_ROOT",
        "/mnt/data01/embedded/projects/wangyutai/yf_feigang/test_images_full_car",
    )
    remote_scp_timeout_sec: int = 1800


@dataclass
class YongfengDatasetUploadConfig:
    """永锋废钢数据集上传（赛信金睛视觉 AI 开发平台）配置。"""

    base_url: str = _env(
        "YONGFENG_UPLOAD_BASE_URL",
        "http://pre-athena-mv.cisdigital.cn",
    )
    username: str = _env("YONGFENG_UPLOAD_USERNAME")
    password: str = _env("YONGFENG_UPLOAD_PASSWORD")
    namespace: int = int(_env("YONGFENG_UPLOAD_NAMESPACE", "1"))
    dataset_name_prefix: str = "永锋-"
    dataset_search_keyword: str = "永锋"
    impurity_dataset_name: str = _env(
        "YONGFENG_UPLOAD_IMPURITY_NAME",
        "永锋废钢杂质分割",
    )
    impurity_dataset_id: int = int(
        _env("YONGFENG_UPLOAD_IMPURITY_ID", "28392")
    )
    login_timeout: float = 15.0
    request_timeout: float = 30.0
    minio_timeout: float = 1800.0
    upload_timeout: float = 300.0
    job_poll_interval: float = 5.0
    job_poll_max_attempts: int = 120


@dataclass
class VPNConfig:
    """aTrust VPN 配置。"""

    gateway: str = _env(
        "YONGFENG_VPN_GATEWAY",
        "https://atrust.lg.china-yongfeng.com:1443",
    )
    username: str = _env("YONGFENG_VPN_USERNAME")
    password: str = _env("YONGFENG_VPN_PASSWORD")
    check_url: str = _env(
        "YONGFENG_VPN_CHECK_URL",
        "http://vision.lg.china-yongfeng.com/packing-tape/",
    )
    timeout: float = 5.0


@dataclass
class LLMConfig:
    """对话模型配置（默认 DeepSeek，OpenAI 兼容接口）。"""

    api_key: str = _env("DEEPSEEK_API_KEY") or _env("ZHIPU_API_KEY")
    base_url: str = _env("LLM_BASE_URL", "https://api.deepseek.com")
    model: str = _env("LLM_MODEL", "deepseek-chat")


@dataclass
class TencentDocsConfig:
    """腾讯文档开放接口配置。"""

    access_token: str = _env("TENCENT_DOCS_ACCESS_TOKEN")
    client_id: str = _env("TENCENT_DOCS_CLIENT_ID")
    open_id: str = _env("TENCENT_DOCS_OPEN_ID")
    enabled: bool = _env("TENCENT_DOCS_ENABLED", "0") == "1"


@dataclass
class YongfengConfig:
    """永锋烧结矿准确率配置。"""

    analysis_base_url: str = _env(
        "YONGFENG_ANALYSIS_BASE_URL",
        "http://qbee.lg.china-yongfeng.com/",
    )
    analysis_query_base_url: str = _env(
        "YONGFENG_ANALYSIS_QUERY_BASE_URL",
        "http://iron.lg.china-yongfeng.com/",
    )
    login_endpoint: str = "/auth/api/sysmgr/sso/login"
    account: str = _env("YONGFENG_ACCOUNT")
    password: str = _env("YONGFENG_PASSWORD")
    visual_1_base_url: str = _env(
        "YONGFENG_VISUAL_1_BASE_URL",
        "http://vision.lg.china-yongfeng.com",
    )
    visual_2_base_url: str = _env(
        "YONGFENG_VISUAL_2_BASE_URL",
        "http://vision.lg.china-yongfeng.com",
    )
    visual_username: str = _env("YONGFENG_VISUAL_USERNAME")
    visual_password: str = _env("YONGFENG_VISUAL_PASSWORD")
    analysis_token: str = _env("YONGFENG_ANALYSIS_TOKEN")
    api_code: str = _env("YONGFENG_API_CODE")


@dataclass
class Settings:
    vision: VisionConfig = field(default_factory=VisionConfig)
    vpn: VPNConfig = field(default_factory=VPNConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tencent_docs: TencentDocsConfig = field(default_factory=TencentDocsConfig)
    yongfeng: YongfengConfig = field(default_factory=YongfengConfig)
    scrap: ScrapConfig = field(default_factory=ScrapConfig)
    shenglong: ShenglongConfig = field(default_factory=ShenglongConfig)
    yongfeng_scrap: YongfengScrapConfig = field(default_factory=YongfengScrapConfig)
    yongfeng_upload: YongfengDatasetUploadConfig = field(
        default_factory=YongfengDatasetUploadConfig
    )
    analysis_token: str = _env("ANALYSIS_TOKEN")
    analysis_api_code: str = _env("ANALYSIS_API_CODE")

    def validate(self) -> list[str]:
        """返回阻止应用启动的配置问题。"""
        issues = []
        if not self.llm.api_key:
            issues.append("缺少 DEEPSEEK_API_KEY 或 ZHIPU_API_KEY")
        return issues


settings = Settings()
