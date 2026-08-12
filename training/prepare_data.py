from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_FIELDS = ("instruction", "input", "output")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_and_clean(path: Path) -> tuple[list[dict], int]:
    unique: dict[str, dict] = {}
    rejected = 0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {number} 行不是合法 JSON") from exc
        normalized = {key: str(item.get(key, "")).strip() for key in REQUIRED_FIELDS}
        if not all(normalized.values()):
            rejected += 1
            continue
        key = hashlib.sha256(
            f"{normalized['instruction']}\n{normalized['input']}".encode("utf-8")
        ).hexdigest()
        unique.setdefault(key, normalized)
    return list(unique.values()), rejected


def stratified_split(items: list[dict], ratios: tuple[float, float, float], seed: int) -> dict[str, list[dict]]:
    if abs(sum(ratios) - 1.0) > 1e-9 or min(ratios) <= 0:
        raise ValueError("train/validation/test 比例必须均大于 0 且总和为 1")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        grouped[item["output"]].append(item)
    rng = random.Random(seed)
    result = {"train": [], "validation": [], "test": []}
    for label_items in grouped.values():
        rng.shuffle(label_items)
        total = len(label_items)
        train_end = round(total * ratios[0])
        validation_end = train_end + round(total * ratios[1])
        result["train"].extend(label_items[:train_end])
        result["validation"].extend(label_items[train_end:validation_end])
        result["test"].extend(label_items[validation_end:])
    for values in result.values():
        rng.shuffle(values)
    return result


def write_jsonl(path: Path, items: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="清洗、去重并分层切分 LoRA 数据集")
    parser.add_argument("--config", default="training/config.json")
    args = parser.parse_args()
    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    source = Path(config["source_dataset"])
    output_dir = Path(config["split_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    items, rejected = load_and_clean(source)
    splits = stratified_split(
        items,
        (
            float(config["train_ratio"]),
            float(config["validation_ratio"]),
            float(config["test_ratio"]),
        ),
        int(config["seed"]),
    )
    paths = {}
    for name, values in splits.items():
        path = output_dir / f"{name}.jsonl"
        write_jsonl(path, values)
        paths[name] = path
    manifest = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "seed": config["seed"],
        "source": str(source),
        "sourceSha256": digest(source),
        "sourceRows": len(source.read_text(encoding="utf-8").splitlines()),
        "deduplicatedRows": len(items),
        "rejectedRows": rejected,
        "splits": {
            name: {
                "path": str(path),
                "rows": len(splits[name]),
                "sha256": digest(path),
                "labels": dict(Counter(item["output"] for item in splits[name])),
            }
            for name, path in paths.items()
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
