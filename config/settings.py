"""
项目配置
敏感信息建议通过环境变量注入，此处默认值仅用于开发阶段。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class VisionConfig:
    """永锋视觉系统配置（打包带项目）"""

    base_url: str = "http://vision.lg.china-yongfeng.com/packing-tape"
    username: str = "admin"
    password: str = "Cisdi_mv@8888"

    # F12 Headers 确认的完整路径（base_url 已包含 /packing-tape）
    login_endpoint: str = "/api/record/doLogin"
    history_endpoint: str = "/api/monitor/query-condition"
    statistic_endpoint: str = "/api/monitor/statistic"


@dataclass
class ScrapConfig:
    """废钢检判系统配置

    · 部署现场：镔鑫钢铁（与打包带 VPN 独立，走不同的专网）
    · 系统名：睿视废钢智能质检系统
    · 登录协议：POST form-urlencoded 明文密码 → 设置 Cookie satoken
      后续请求前端同时带 Cookie `satoken` 和 Header `token`，值相同
    """

    base_url: str = "http://172.31.1.102:8081"
    employee_id: str = "022499"
    password: str = "0123456"

    login_endpoint: str = "/fcs/auth/login"
    list_endpoint: str = "/fcs/intelligence/intelliTaskInfo/page"
    detail_endpoint: str = "/fcs/intelligence/intelliTaskInfo/getCheckDetail"

    # 统计过滤：人工主料型为以下 steelType 的车次不参与统计（仅保留在表格）
    # 2=杂摸, 4=中废（字典见 agent/scrap/dict.py）
    exclude_steel_types: tuple = (2, 4)

    # 业务目标值（用于"达标/未达标"判定）
    target_accuracy: float = 0.95
    target_avg_error_rate: float = 0.10
    target_weight_diff_kg: float = 100.0
    target_weight_ratio_lower: float = 0.5
    target_weight_ratio_upper: float = 1.5


@dataclass
class ShenglongConfig:
    """盛隆废钢检判系统配置

    · 部署现场：盛隆钢铁（与镔鑫、打包带 VPN 均独立，走第三张专网）
    · 系统名：睿视废钢智能质检系统（与镔鑫同产品，但后端不同部署）
    · 登录协议：POST + query string + Content-Length:0（镔鑫是 form-urlencoded body）
      token 位置：`data.tokenInfo.tokenValue`（镔鑫是 `data.tokenValue`）
      Cookie 名：`scrape-steel-token`（镔鑫是 `satoken`）
      后续请求同时带 Cookie + Header `token`，值相同
    """

    base_url: str = "http://172.16.16.101:3000"
    employee_id: str = "022499"
    password: str = "0123456"

    login_endpoint: str = "/api/auth/login"
    list_endpoint: str = "/api/intelligence/intelliTaskInfo/page"
    detail_endpoint: str = "/api/intelligence/intelliTaskInfo/getCheckDetail"

    cookie_name: str = "scrape-steel-token"

    # 业务目标值（图3注释）
    target_recognition_rate: float = 0.92  # 主料识别率 R 目标 ≥92%
    target_deduction_compliance_rate: float = 0.90  # 扣杂符合率目标 ≥90%
    # 扣杂准确判定：0.5 ≤ 比值 ≤ 1.5 OR |误差| ≤ 0.15t
    deduction_ratio_lower: float = 0.5
    deduction_ratio_upper: float = 1.5
    deduction_error_tolerance_ton: float = 0.15


@dataclass
class VPNConfig:
    """aTrust VPN 配置"""

    gateway: str = "https://atrust.lg.china-yongfeng.com:1443"
    username: str = "杨雨昕"
    password: str = "cisdi123@8888"
    check_url: str = "http://vision.lg.china-yongfeng.com/packing-tape/"
    timeout: float = 5.0


@dataclass
class LLMConfig:
    """智谱 GLM 配置"""

    api_key: str = os.getenv("ZHIPU_API_KEY", "")
    model: str = "glm-4-flash"


@dataclass
class Settings:
    vision: VisionConfig = field(default_factory=VisionConfig)
    vpn: VPNConfig = field(default_factory=VPNConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    scrap: ScrapConfig = field(default_factory=ScrapConfig)
    shenglong: ShenglongConfig = field(default_factory=ShenglongConfig)

    def validate(self) -> list[str]:
        """检查配置完整性，返回缺失项列表"""
        issues = []
        if not self.llm.api_key:
            issues.append("未配置 ZHIPU_API_KEY 环境变量")
        return issues


settings = Settings()
