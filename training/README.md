# LoRA 训练与独立评测

这套脚本把“有 2000+ 条数据并做过 LoRA”变成可复现的工程资产。原始数据不会直接随机切成仅训练/验证两份，而是先按输入内容哈希去重，再按标签分层切成 `80%/10%/10%`，其中 test split 全程不参与训练和选模。

```powershell
.\.venv\Scripts\python.exe training\prepare_data.py
pip install -r requirements-training.txt
python training\train_lora.py
python training\evaluate_classifier.py
```

`data/lora/splits/manifest.json` 记录随机种子、源文件 SHA-256、去重数量、各 split 校验和与标签分布。训练产物保存完整配置；评测输出包含准确率和混淆矩阵。训练需要支持 BF16 的 CUDA 环境；显存不足时应使用 Linux/CUDA 下的 QLoRA，而不是在应用进程里训练。

注意：这份合成数据只能证明训练链路可复现，不能替代心理专业人员标注、跨来源外部测试集、误判审查与上线前伦理评估。
