# Implementation Path

## Goal

Build a domain-specific retrieval and code generation system for vehicle
diagnostic development without copying HuixiangDou as a whole.

## Phase 1

Build a minimal closed loop:

1. `MinerU -> normalized knowledge units`
2. `knowledge units -> retrieval corpus`
3. `retrieval context -> Qwen DSL generation`
4. `DSL -> rule validation`
5. `validated DSL -> C template rendering`

## Phase 2

Introduce retrieval engineering:

- dense retrieval for protocol documents
- sparse retrieval for historical code templates
- metadata filters for protocol, ECU, service, and DID
- reranker after candidate merge

## Phase 3

Introduce serving and benchmark:

- one vLLM worker per 3090 as the default online topology
- compare single instance, dual instance, and TP=2
- track TTFT, TPOT, throughput, P95, and validation pass rate

## Phase 4

Introduce selective graph retrieval:

- extract stable entities and relations from normalized knowledge
- keep graph storage lightweight first
- add graph retrieval only when dense+sparse retrieval plateaus

## Selective References

Use from HuixiangDou:

- hybrid retrieval organization
- dense and sparse split
- build-store mindset

Do not use from HuixiangDou:

- chat frontend integrations
- chat rejection logic for group messages
- web search as a default path

Use from MinerU:

- PDF parsing
- OCR fallback
- `content_list.json` and `middle.json` as stable intermediate outputs

