"""路径和安全检查管理"""
import os
from typing import Optional

from .constants import MODEL_VARIANTS


class PathManager:
    """工作路径管理、白名单和安全检查"""

    def __init__(self, config: dict):
        self.config = config
        self._recent_paths: list[dict] = []

    @property
    def default_workdir(self) -> str:
        ws_cfg = self.config.get("workspace_config", {})
        return ws_cfg.get("default_workdir", "").strip() or os.getcwd()

    @property
    def allowed_workdirs(self) -> list:
        ws_cfg = self.config.get("workspace_config", {})
        return ws_cfg.get("allowed_workdirs", []) or []

    @property
    def check_path_safety(self) -> bool:
        ws_cfg = self.config.get("workspace_config", {})
        return ws_cfg.get("check_path_safety", True)

    def normalize_path(self, path: str) -> str:
        if not path:
            return path
        return os.path.abspath(os.path.expanduser(path)).rstrip("/\\")

    def is_path_allowed(self, path: str) -> bool:
        if not self.check_path_safety and not self.allowed_workdirs:
            return True

        norm_path = self.normalize_path(path)
        if not norm_path:
            return False

        if not self.allowed_workdirs:
            default = self.normalize_path(self.default_workdir)
            return norm_path == default or norm_path.startswith(default + os.sep)

        for allowed in self.allowed_workdirs:
            norm_allowed = self.normalize_path(allowed)
            if norm_path == norm_allowed or norm_path.startswith(norm_allowed + os.sep):
                return True
        return False

    def add_recent_path(self, path: str):
        norm_path = self.normalize_path(path)
        for entry in self._recent_paths:
            if self.normalize_path(entry.get("path", "")) == norm_path:
                entry["used_count"] = entry.get("used_count", 0) + 1
                return
        self._recent_paths.insert(0, {"path": norm_path, "used_count": 1})
        self._recent_paths = self._recent_paths[:50]

    def get_recent_paths(self, limit: int = 10) -> list[str]:
        return [e["path"] for e in self._recent_paths[:limit]]

    def get_allowed_dirs_with_recent(self) -> list[str]:
        result = []
        for d in self.allowed_workdirs:
            norm = self.normalize_path(d)
            if norm not in result:
                result.append(norm)
        for rp in self.get_recent_paths():
            if rp not in result:
                result.append(rp)
        return result


class ModelManager:
    """模型和思考等级管理"""

    def __init__(self, config: dict):
        self.config = config

    @property
    def default_model(self) -> str:
        mc = self.config.get("model_config", {})
        return mc.get("default_model", "").strip()

    @property
    def default_variant(self) -> str:
        mc = self.config.get("model_config", {})
        return mc.get("default_variant", "").strip()

    def parse_model(self, model_str: str) -> tuple[Optional[str], Optional[str]]:
        if not model_str or not model_str.strip():
            return (None, None)
        parts = model_str.strip().split("/", 1)
        if len(parts) == 2:
            return (parts[0], parts[1])
        return (None, parts[0])

    def build_model_body(self, model_str: str) -> dict:
        """Build model dict for API: {providerID, modelID}. Variant is a separate body field."""
        body = {}
        provider_id, model_id = self.parse_model(model_str)
        if provider_id and model_id:
            body["providerID"] = provider_id
            body["modelID"] = model_id
        elif model_id:
            body["modelID"] = model_id
        return body

    def validate_variant(self, variant: str) -> bool:
        return variant in MODEL_VARIANTS
