import re


class PrivacySanitizer:
    patterns = [
        re.compile(r"1[3-9]\d{9}"),
        re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        re.compile(r"\b\d{17}[\dXx]\b"),
        re.compile(r"(?i)(?:学号|student\s*id)[：:\s]*[A-Za-z0-9_-]{6,20}"),
        re.compile(r"(?:姓名|我叫|名字是)[：:\s]*[\u4e00-\u9fff·]{2,12}"),
        re.compile(r"(?:微信|微信号|wechat|QQ)[：:\s]*[A-Za-z0-9_-]{5,24}", re.IGNORECASE),
        re.compile(r"(?:宿舍|寝室)[：:\s]*(?:[A-Za-z0-9一二三四五六七八九十号楼栋区-]{1,20})(?:室|房间)?"),
        re.compile(r"(?:住址|家庭地址|现住址|地址)[：:\s]*[^，。；;\n]{4,80}"),
    ]

    def sanitize(self, text: str) -> str:
        sanitized = text or ""
        for pattern in self.patterns:
            sanitized = pattern.sub("[已脱敏]", sanitized)
        return sanitized

    def contains_sensitive(self, text: str) -> bool:
        return any(pattern.search(text or "") for pattern in self.patterns)
