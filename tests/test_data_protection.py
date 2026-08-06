import unittest
from types import SimpleNamespace

try:
    from cryptography.fernet import Fernet
except ModuleNotFoundError:
    Fernet = None

from app.services.data_protection import SensitiveTextProtector


class SensitiveTextProtectorTests(unittest.TestCase):
    @unittest.skipUnless(Fernet is not None, "当前测试环境未安装 cryptography")
    def test_configured_fernet_key_encrypts_and_round_trips(self):
        original = "学生手机号已在进入存储前脱敏"
        protector = SensitiveTextProtector(
            SimpleNamespace(sensitive_data_encryption_key=Fernet.generate_key().decode("ascii"))
        )

        protected = protector.protect(original)

        self.assertTrue(protected.startswith("enc:v1:"))
        self.assertNotIn(original, protected)
        self.assertEqual(protector.reveal(protected), original)

    def test_missing_key_preserves_legacy_plaintext_but_cannot_reveal_ciphertext(self):
        protector = SensitiveTextProtector(SimpleNamespace(sensitive_data_encryption_key=""))

        self.assertEqual(protector.protect("普通文本"), "普通文本")
        self.assertEqual(protector.reveal("enc:v1:unknown"), "[加密内容不可用]")


if __name__ == "__main__":
    unittest.main()
