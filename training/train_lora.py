from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="训练 Qwen2.5-7B MindBridge LoRA")
    parser.add_argument("--config", default="training/config.json")
    parser.add_argument("--resume-from-checkpoint", default=None)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))

    from datasets import load_dataset
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, DataCollatorForSeq2Seq, Trainer, TrainingArguments, set_seed

    set_seed(int(config["seed"]))
    split_dir = Path(config["split_dir"])
    dataset = load_dataset(
        "json",
        data_files={
            "train": str(split_dir / "train.jsonl"),
            "validation": str(split_dir / "validation.jsonl"),
        },
    )
    tokenizer = AutoTokenizer.from_pretrained(config["base_model"], trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        config["base_model"],
        torch_dtype="auto",
        device_map="auto",
        trust_remote_code=True,
    )
    model = get_peft_model(
        model,
        LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=int(config["lora_rank"]),
            lora_alpha=int(config["lora_alpha"]),
            lora_dropout=float(config["lora_dropout"]),
            target_modules=list(config["target_modules"]),
            bias="none",
        ),
    )

    def tokenize(item: dict) -> dict:
        prompt_messages = [
            {"role": "system", "content": item["instruction"]},
            {"role": "user", "content": item["input"]},
        ]
        full_messages = [*prompt_messages, {"role": "assistant", "content": item["output"]}]
        prompt = tokenizer.apply_chat_template(prompt_messages, tokenize=False, add_generation_prompt=True)
        full = tokenizer.apply_chat_template(full_messages, tokenize=False, add_generation_prompt=False)
        encoded = tokenizer(full, truncation=True, max_length=int(config["max_length"]))
        prompt_ids = tokenizer(prompt, truncation=True, max_length=int(config["max_length"]))["input_ids"]
        labels = list(encoded["input_ids"])
        labels[: min(len(prompt_ids), len(labels))] = [-100] * min(len(prompt_ids), len(labels))
        encoded["labels"] = labels
        return encoded

    tokenized = dataset.map(tokenize, remove_columns=dataset["train"].column_names)
    output_dir = Path(config["output_dir"])
    trainer = Trainer(
        model=model,
        train_dataset=tokenized["train"],
        eval_dataset=tokenized["validation"],
        data_collator=DataCollatorForSeq2Seq(
            tokenizer=tokenizer,
            padding=True,
            label_pad_token_id=-100,
        ),
        args=TrainingArguments(
            output_dir=str(output_dir),
            num_train_epochs=float(config["epochs"]),
            learning_rate=float(config["learning_rate"]),
            per_device_train_batch_size=int(config["batch_size"]),
            per_device_eval_batch_size=int(config["batch_size"]),
            gradient_accumulation_steps=int(config["gradient_accumulation_steps"]),
            eval_strategy="epoch",
            save_strategy="epoch",
            load_best_model_at_end=True,
            metric_for_best_model="eval_loss",
            greater_is_better=False,
            fp16=False,
            bf16=True,
            logging_steps=10,
            report_to="none",
            seed=int(config["seed"]),
        ),
    )
    trainer.train(resume_from_checkpoint=args.resume_from_checkpoint)
    trainer.save_model(str(output_dir))
    tokenizer.save_pretrained(str(output_dir))
    (output_dir / "training_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
