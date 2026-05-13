# Day 10 — Reliability Engineering for Production Agents

**Học viên:** Đặng Văn Minh  
**MSHV:** 2A202600027  
**Lab:** phase2-track3-day10-reliability-agent (Vin-PracticalAI)  

---

## 1. Giới thiệu lab

Lab xây dựng một **reliability layer kiểu production** cho LLM agent gateway. Một request đi qua 3 lớp bảo vệ trước khi tới provider:

1. **Cache** (in-memory hoặc Redis shared) với hybrid similarity + privacy/false-hit guardrails.
2. **Circuit breaker** thread-safe theo từng provider — fail-fast khi provider lỗi liên tục.
3. **Fallback chain** — primary → backup → static fallback message.

Mục tiêu kiến thức:

- Hiện thực state machine CLOSED / OPEN / HALF_OPEN đúng chuẩn.
- Tránh retry storm khi provider down.
- Đo và báo cáo: availability, P50/P95/P99 latency, fallback success rate, cache hit rate, recovery time, cost saved.
- Mô phỏng chaos bằng nhiều scenario có pass/fail criteria.
- Triển khai shared cache qua Redis Docker cho horizontal scaling.

Báo cáo chi tiết (kiến trúc, số liệu, SLO, failure analysis): xem [reports/final_report.md](reports/final_report.md).

---

## 2. Cây thư mục

```
phase2-track3-day10-reliability-agent/
├── src/reliability_lab/
│   ├── circuit_breaker.py   # State machine 3 trạng thái, thread-safe bằng threading.RLock
│   ├── gateway.py           # ReliabilityGateway: cache → CB chain → fallback, route reasons chi tiết
│   ├── cache.py             # ResponseCache (hybrid Jaccard token+trigram) + SharedRedisCache + guardrails
│   ├── chaos.py             # Chạy scenarios song song qua ThreadPoolExecutor, evaluate pass/fail, build cache_comparison
│   ├── metrics.py           # RunMetrics Pydantic + percentile + per_scenario + cache_comparison + slo_check()
│   ├── providers.py         # FakeLLMProvider — fail/latency/cost giả lập, không cần API key thật
│   └── config.py            # Pydantic config loader; LoadTestConfig.concurrency, ScenarioConfig.cache_override
│
├── tests/                                   # 36 tests, tất cả pass khi Redis up
│   ├── test_circuit_breaker.py  # 11 tests: state cycle, no-retry-storm, thread-safety
│   ├── test_cache_similarity.py # 9 tests: hybrid score, privacy/false-hit guard, TTL, Redis graceful
│   ├── test_concurrency.py      # 3 tests: 200 req qua 10/20 thread, sequential fallback
│   ├── test_redis_cache.py      # 6 tests: connection, shared state, TTL expiry, privacy, false-hit
│   ├── test_gateway_contract.py # smoke contract, route prefix
│   ├── test_metrics.py          # percentile + report dict shape
│   ├── test_config.py           # YAML load + scenario presence
│   └── test_todo_requirements.py # ex-xfail, giờ pass: cache không false-hit "2024" vs "2026"
│
├── scripts/
│   ├── run_chaos.py         # CLI: load config + run_simulation + ghi metrics.json
│   └── generate_report.py   # CLI: convert metrics.json → reports/auto_report.md (stub)
│
├── configs/
│   └── default.yaml         # 2 providers, CB threshold=3, cache mem/redis, 5 scenarios, concurrency=10, requests=200
│
├── data/
│   └── sample_queries.jsonl # 5 query mẫu (policy / technical / privacy / faq)
│
├── reports/
│   ├── final_report.md      # **Báo cáo chính (VN, 9 sections)** — submission deliverable
│   ├── metrics.json         # Output từ make run-chaos (reproducible)
│   ├── auto_report.md       # Stub auto-generated từ metrics.json
│   └── report_template.md   # Template gốc (giữ làm tham chiếu)
│
├── docs/
│   ├── RUBRIC.md            # Rubric chấm điểm 100đ (giữ nguyên từ starter)
│   └── superpowers/specs/
│       └── 2026-05-13-day10-reliability-lab-design.md  # Execution spec đã follow
│
├── docker-compose.yml       # Redis 7-alpine cho SharedRedisCache (port 6379, AOF persistence)
├── Dockerfile               # Container hoá runtime (không bắt buộc cho grading)
├── Makefile                 # test / lint / typecheck / run-chaos / report / docker-up
├── pyproject.toml           # Python ≥3.10, deps: pydantic / pyyaml / numpy / rich / redis
└── README.md                # File này
```

---

## 3. Công việc đã làm (6 phase, 6 commits)

| Phase | Nội dung chính | Files | Tests thêm |
| ---: | --- | --- | --- |
| 1 | Circuit breaker thread-safe (RLock), state cycle CLOSED↔OPEN↔HALF_OPEN. Gateway timing + route reasons chi tiết (`primary:<name>` / `fallback:<name>` / `cache_hit:<score>` / `static_fallback`). Cache call bọc try/except cho graceful. | `circuit_breaker.py`, `gateway.py`, `chaos.py`, `test_gateway_contract.py` | `test_circuit_breaker.py` (+11) |
| 2 | `RunMetrics.per_scenario` + `cache_comparison` + `slo_check()`. `chaos.evaluate_scenario()` áp pass/fail thật theo từng scenario. Disable cache cho CB-exercising scenarios để CB thấy traffic. | `metrics.py`, `chaos.py`, `config.py`, `default.yaml` | — |
| 3 | `ResponseCache.similarity`: exact-match → `0.5×Jaccard_token + 0.5×Jaccard_trigram`. `get()` áp `_is_uncacheable` + `_looks_like_false_hit` với `false_hit_log`. `set()` skip uncacheable. xfail test → passing. | `cache.py`, `test_todo_requirements.py` | `test_cache_similarity.py` (+8) |
| 4 | `SharedRedisCache.get/set`: exact-match HGET fast path → scan_iter + similarity → false-hit guard. Bọc try/except trả `(None, 0.0)` trên RedisError. `socket_connect_timeout=2` cho graceful nhanh. | `cache.py` | `test_redis_graceful_degradation_when_unreachable` (+1) + 6 Redis tests unskip |
| 5 | `LoadTestConfig.concurrency` + `ThreadPoolExecutor` trong `chaos.run_scenario`. Metrics dùng `Lock` cho thread-safe. `ResponseCache` thêm `RLock`. Auto build `cache_comparison` block khi có `cache_off`+`cache_on`. Tăng `requests` lên 200. | `chaos.py`, `cache.py`, `config.py`, `default.yaml` | `test_concurrency.py` (+3) |
| 6 | Báo cáo VN 9 sections với evidence: shared state demo (2 instances), Redis CLI KEYS/HGETALL, cache comparison delta, SLO table, failure analysis. Sửa Makefile để `make report` ghi `auto_report.md` không đè `final_report.md`. | `reports/final_report.md`, `Makefile` | — |

### Stretch goals đã làm

- ✅ **Concurrency**: `ThreadPoolExecutor(max_workers=10)`, thread-safe CB + metrics + cache.
- ✅ **Redis graceful degradation**: `socket_connect_timeout=2`, mọi Redis call bọc `try/except` trả cache-miss, không crash gateway.
- ✅ **SLO definition**: bảng SLI/SLO/Actual/Met trong final_report.

### Kết quả đo (1000 requests, 5 scenarios, concurrency=10)

| Metric | Value |
|---|---:|
| availability | 96.5% |
| latency P50 / P95 / P99 | 271 / 493 / 538 ms |
| fallback_success_rate | 93.8% |
| cache_hit_rate (overall) | 15.0% |
| circuit_open_count | 9 |
| recovery_time_ms | 2220 |
| estimated_cost_saved | $0.15 |
| **5/5 scenarios** | **pass** |

**Cache comparison** (cache_off vs cache_on, same providers):

| Metric | Cache off | Cache on | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 288 | 0.31 | **−287.93 ms (−99.9%)** |
| latency_p95_ms | 507 | 319 | **−188 ms (−37%)** |
| estimated_cost | $0.0769 | $0.0214 | **−$0.0554 (−72%)** |
| cache_hit_rate | 0 | 0.75 | +0.75 |

---

## 4. Hướng dẫn chấm bài

### Yêu cầu môi trường

- Python ≥ 3.10
- Docker Desktop (để chạy Redis)
- macOS / Linux (đã test trên macOS Darwin 25.3, Python 3.13)

### Reproduce kết quả

```bash
# 1. Cài deps
python -m pip install -e ".[dev]"

# 2. Khởi động Redis (cho SharedRedisCache + Redis tests)
docker compose up -d
docker compose ps   # confirm "redis  Up (healthy)"

# 3. Chạy toàn bộ test suite
make test
# Kỳ vọng: 36 passed in ~6s (tất cả Redis tests pass, không skip)

# 4. Chạy chaos simulation → tạo reports/metrics.json
make run-chaos
# Kỳ vọng: ~25-30s, 5 scenarios chạy concurrent qua 10 threads, file metrics.json có per_scenario + cache_comparison

# 5. Xem báo cáo
open reports/final_report.md   # macOS
# hoặc: cat reports/final_report.md
```

### Verify từng phần

```bash
# Type check
make typecheck

# Lint
make lint

# Auto-generated stub (không phải báo cáo chính, đừng đè final_report.md)
make report   # → reports/auto_report.md

# Switch sang Redis backend (tuỳ chọn)
# Sửa configs/default.yaml: cache.backend: redis
make run-chaos
docker compose exec redis redis-cli KEYS "rl:cache:*"   # xem cached entries
```

### Mapping deliverable ↔ rubric

| Rubric category | Điểm | Bằng chứng |
|---|---:|---|
| Circuit breaker & fallback | 25 | [src/reliability_lab/circuit_breaker.py](src/reliability_lab/circuit_breaker.py), [tests/test_circuit_breaker.py](tests/test_circuit_breaker.py), route reasons chi tiết trong [gateway.py](src/reliability_lab/gateway.py) |
| In-memory cache & cost | 15 | Hybrid similarity + guardrails [cache.py:44-130](src/reliability_lab/cache.py#L44-L130), [tests/test_cache_similarity.py](tests/test_cache_similarity.py), bảng cache comparison trong `metrics.json` |
| Redis shared cache | 15 | [SharedRedisCache](src/reliability_lab/cache.py#L92-L195), 6/6 [tests/test_redis_cache.py](tests/test_redis_cache.py) pass, graceful degradation test, evidence shared state trong [final_report.md §6](reports/final_report.md#6-redis-shared-cache) |
| Observability & metrics | 15 | [metrics.py](src/reliability_lab/metrics.py) với `per_scenario` + `cache_comparison` + `slo_check`, [reports/metrics.json](reports/metrics.json) reproducible |
| Chaos & load testing | 15 | 5 named scenarios với pass/fail thật trong [chaos.py:`evaluate_scenario`](src/reliability_lab/chaos.py), concurrency 10 threads, recovery_time_ms thật từ transition_log |
| Report & code quality | 15 | [reports/final_report.md](reports/final_report.md) 9 sections, type hints toàn project, 36 tests pass, ruff lint clean |

### Files quan trọng cho grader

1. **[reports/final_report.md](reports/final_report.md)** — báo cáo chính (đọc trước).
2. **[reports/metrics.json](reports/metrics.json)** — số liệu raw (kiểm chứng với báo cáo).
3. **[src/reliability_lab/](src/reliability_lab/)** — code production.
4. **[tests/](tests/)** — chạy `make test` để verify.
5. **[docs/superpowers/specs/2026-05-13-day10-reliability-lab-design.md](docs/superpowers/specs/2026-05-13-day10-reliability-lab-design.md)** — execution spec đã follow.

### Liên hệ

Có thắc mắc về submission xin liên hệ:
minhdv0201@gmail.com
