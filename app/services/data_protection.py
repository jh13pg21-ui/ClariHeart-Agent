from __future__ import annotations


class SensitiveTextProtector:
    """可选的应用层静态加密；未配置密钥时保持向后兼容。"""

    prefix = "enc:v1:"

    def __init__(self, settings):
        self.key = str(getattr(settings, "sensitive_data_encryption_key", "") or "").strip()
        self._fernet = None
        if self.key:
            try:
                from cryptography.fernet import Fernet
            except ModuleNotFoundError as exc:
                raise RuntimeError("启用敏感数据加密前必须安装 requirements.txt 中的 cryptography") from exc
            try:
                self._fernet = Fernet(self.key.encode("ascii"))
            except Exception as exc:
                raise ValueError("SENSITIVE_DATA_ENCRYPTION_KEY 必须是合法 Fernet key") from exc

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def protect(self, text: str) -> str:
        value = str(text or "")
        if not self._fernet or value.startswith(self.prefix):
            return value
        return self.prefix + self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def reveal(self, text: str) -> str:
        value = str(text or "")
        if not value.startswith(self.prefix):
            return value
        if not self._fernet:
            return "[加密内容不可用]"
        try:
            return self._fernet.decrypt(value.removeprefix(self.prefix).encode("ascii")).decode("utf-8")
        except Exception:
            return "[加密内容损坏]"
