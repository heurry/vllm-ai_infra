# EMBEDDING_DEVICE=cuda:0 EMBEDDING_MAX_SEQ_LENGTH=512 .venv/bin/python scripts/build_vector_index.py \
#   --index-dir data/index/treg_20260402_semantic_hybrid \
#   --collection-name diagnostic_knowledge_treg_20260402_semantic_hybrid \
#   --milvus-uri http://127.0.0.1:19530 \
#   --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B \
#   --metric-type COSINE \
#   --batch-size 16 \
#   --drop-existing

# vllm serve /media/xdu/新加卷/LLM_model/Qwen3.6-27B-FP8 \
# --host 0.0.0.0 \
# --port 8000 \
# --tensor-parallel-size 2 \
# --max-model-len 32768 \
# --gpu-memory-utilization 0.90 \
# --max-num-seqs 1 \
# --max-num-batched-tokens 8192 \
# --dtype float16 \
# --reasoning-parser qwen3 \
# --language-model-only \
# --trust-remote-code --enforce-eager \
# --enable-prefix-caching

#启动Milvus服务
docker start milvus-etcd
docker start milvus-minio
docker start milvus-standalone



#构建向量索引
EMBEDDING_DEVICE=cuda:0 EMBEDDING_MAX_SEQ_LENGTH=512 .venv/bin/python scripts/build_vector_index.py \
  --index-dir data/index/treg_20260402_semantic_hybrid \
  --collection-name diagnostic_knowledge_treg_20260402_semantic_hybrid \
  --milvus-uri http://127.0.0.1:19530 \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B \
  --metric-type COSINE \
  --batch-size 1024 \
  --milvus-timeout-seconds 10 \
  --drop-existing

#启动vllm服务
ln -sfn "/media/xdu/新加卷/LLM_model/Qwen3.6-27B-FP8" /tmp/Qwen3_5_27B_FP8
.venv/bin/vllm serve /tmp/Qwen3_5_27B_FP8 \
  --served-model-name "/media/xdu/新加卷/LLM_model/Qwen3.6-27B-FP8" \
  --hf-config-path /tmp/Qwen3_5_27B_FP8 \
  --model-impl vllm \
  --host 0.0.0.0 \
  --port 8000 \
  --tensor-parallel-size 2 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --max-num-seqs 1 \
  --max-num-batched-tokens 8192 \
  --dtype float16 \
  --reasoning-parser qwen3 \
  --language-model-only \
  --trust-remote-code \
  --enforce-eager \
  --enable-prefix-caching


#运行工作流
EMBEDDING_DEVICE=cpu .venv/bin/python scripts/run_workload_pipeline.py \
  --workload configs/workloads/treg_20260402_semantic_hybrid_llm.json \
  --enable-llm \
  --print-steps \
  --progress \
  --skip-step build_vector_index \
  --skip-step resolve_audit_with_llm \
  --enable-dense \
  --vector-config configs/retrieval/milvus_vector.json \
  --vector-collection diagnostic_knowledge_treg_20260402_semantic_hybrid \
  --milvus-uri http://127.0.0.1:19530 \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B

EMBEDDING_DEVICE=cpu .venv/bin/python scripts/run_workload_pipeline.py \
  --workload configs/workloads/treg_20260402_semantic_hybrid_llm.json \
  --enable-llm \
  --print-steps \
  --progress \
  --skip-step build_vector_index \
  --skip-step generate_xml_plan \
  --skip-step resolve_audit_with_llm \
  --enable-dense \
  --vector-config configs/retrieval/milvus_vector.json \
  --vector-collection diagnostic_knowledge_treg_20260402_semantic_hybrid \
  --milvus-uri http://127.0.0.1:19530 \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B
