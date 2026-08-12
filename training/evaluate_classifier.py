from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


LABELS = ("正常", "焦虑", "低落", "高风险")


def main() -> int:
    parser = argparse.ArgumentParser(description="在独立 test split 上评估 LoRA 分类器")
    parser.add_argument("--config", default="training/config.json")
    parser.add_argument("--adapter", default=None)
    parser.add_argument("--output", default="target/lora-classifier-eval.json")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    adapter = args.adapter or config["output_dir"]
    tokenizer = AutoTokenizer.from_pretrained(config["base_model"], trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        config["base_model"], torch_dtype="auto", device_map="auto", trust_remote_code=True
    )
    model = PeftModel.from_pretrained(model, adapter)
    model.eval()
    cases = [json.loads(line) for line in (Path(config["split_dir"]) / "test.jsonl").read_text(encoding="utf-8").splitlines()]
    confusion = Counter()
    results = []
    for case in cases:
        messages = [
            {"role": "system", "content": case["instruction"]},
            {"role": "user", "content": case["input"]},
        ]
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        encoded = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            generated = model.generate(**encoded, max_new_tokens=8, do_sample=False)
        answer = tokenizer.decode(generated[0, encoded["input_ids"].shape[1]:], skip_special_tokens=True).strip()
        predicted = next((label for label in LABELS if label in answer), "未知")
        expected = case["output"]
        confusion[(expected, predicted)] += 1
        results.append({"expected": expected, "predicted": predicted, "raw": answer})
    accuracy = sum(item["expected"] == item["predicted"] for item in results) / max(1, len(results))
    report = {
        "totalCases": len(results),
        "accuracy": accuracy,
        "confusion": {
            expected: {predicted: confusion[(expected, predicted)] for predicted in (*LABELS, "未知")}
            for expected in LABELS
        },
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"totalCases": len(results), "accuracy": accuracy}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
