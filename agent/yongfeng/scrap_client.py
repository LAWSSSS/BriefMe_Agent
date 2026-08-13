"""永锋废钢检判系统客户端；独立于盛隆客户端。"""
from __future__ import annotations

from typing import Any
import requests

from config.settings import settings
from .scrap_models import YongfengRecord


class YongfengScrapClient:
    def __init__(self) -> None:
        cfg = settings.yongfeng_scrap
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0",
        })
        self.token: str | None = None

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "YongfengScrapClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def login(self) -> None:
        resp = self.session.post(
            f"{self.base_url}{self.cfg.login_endpoint}",
            params={"employeeId": self.cfg.employee_id, "password": self.cfg.password},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "0"},
            data=b"",
            timeout=60,
            verify=False,
        )
        resp.raise_for_status()
        body = resp.json()
        if not (body.get("meta") or {}).get("success"):
            raise RuntimeError(f"永锋登录失败: {body}")
        self.token = ((body.get("data") or {}).get("tokenInfo") or {}).get("tokenValue")
        if not self.token:
            raise RuntimeError("永锋登录成功但未返回 token")
        self.session.cookies.set(self.cfg.cookie_name, self.token)

    def _headers(self) -> dict[str, str]:
        return {"token": self.token or ""}

    def _request_json(self, method: str, endpoint: str, **kwargs: Any) -> dict:
        if not self.token:
            self.login()
        resp = self.session.request(method, f"{self.base_url}{endpoint}", headers=self._headers(), timeout=60, verify=False, **kwargs)
        resp.raise_for_status()
        body = resp.json()
        meta = body.get("meta") or {}
        if not meta.get("success"):
            raise RuntimeError(f"永锋接口失败: {meta.get('message')}")
        return body

    def query_list_by_date(self, date_str: str) -> list[YongfengRecord]:
        body = self._request_json("GET", self.cfg.list_endpoint, params={
            "current": 1, "size": 200,
            "startTime": f"{date_str} 00:00:00",
            "endTime": f"{date_str} 23:59:59",
        })
        records = ((body.get("data") or {}).get("records") or [])
        return [YongfengRecord.from_dict(item) for item in records]

    def get_detail_by_flow(self, flow_code: str) -> dict:
        body = self._request_json("GET", self.cfg.detail_endpoint, params={"flowCode": flow_code})
        return body.get("data") or {}
