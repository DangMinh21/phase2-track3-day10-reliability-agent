# Day 10 — Báo cáo Reliability Engineering cho LLM Agent

> Ngày chạy: 2026-05-13. Cấu hình: `configs/default.yaml`. 1000 requests trên 5 chaos scenarios, concurrency=10.

## 1. Tóm tắt kiến trúc

`ReliabilityGateway` định tuyến mỗi request qua 3 lớp bảo vệ — cache (memory hoặc Redis chia sẻ), circuit breaker theo từng provider, và fallback chain. Mỗi `CircuitBreaker` là state machine 3 trạng thái (CLOSED / OPEN / HALF_OPEN) thread-safe nhờ `threading.RLock`, mở khi `failure_count ≥ failure_threshold`, đóng lại sau khi `success_threshold` probe ở HALF_OPEN. Cache áp dụng hybrid similarity (exact-match → `0.5×Jaccard_token + 0.5×Jaccard_trigram`) cộng với privacy filter và false-hit detector cho query có 4-digit khác nhau (năm, ID). Load test chạy qua `ThreadPoolExecutor` để mô phỏng traffic concurrent.

```
                          ┌──────────────────────────┐
   User request ──────►   │  ReliabilityGateway       │
                          │  .complete(prompt)        │
                          └──────────┬───────────────┘
                                     │ time.perf_counter() bắt đầu
                                     ▼
            ┌─── HIT (score>=thresh, not false-hit) ───► return cache_hit:<score>
            │
   [Cache lookup] (memory ResponseCache HOẶC SharedRedisCache)
            │ MISS / privacy skip / false-hit guard
            ▼
   [CircuitBreaker: primary]
            │ allow_request() == True ─► provider.complete() ─► return primary:<name>
            │ OPEN  ─► CircuitOpenError ─► thử provider tiếp theo
            ▼
   [CircuitBreaker: backup]
            │ allow_request() == True ─► provider.complete() ─► return fallback:<name>
            │ OPEN ─► CircuitOpenError ─► tiếp
            ▼
   [Static fallback]  ─► "The service is temporarily degraded..."
```

Khi response trả về, gateway lưu vào cache (với guardrails) trước khi return. Tất cả Redis call bọc try/except để fail-soft (Redis down → treat as cache miss, gateway vẫn chạy).

## 2. Cấu hình

| Setting | Giá trị | Lý do |
|---|---:|---|
| `providers.primary.fail_rate` | 0.25 | Mô phỏng provider sản xuất với tỉ lệ lỗi điển hình (~25% timeout/rate-limit) |
| `providers.backup.fail_rate` | 0.05 | Backup ổn định hơn (provider thứ 2 thường ít bị ảnh hưởng cùng lúc) |
| `providers.*.base_latency_ms` | 180/260 | Tách biệt để fallback có thể nhận diện được trên biểu đồ latency |
| `circuit_breaker.failure_threshold` | 3 | Đủ nhỏ để phát hiện lỗi nhanh, đủ lớn để tránh open vì jitter ngẫu nhiên |
| `circuit_breaker.reset_timeout_seconds` | 2.0 | Khớp với recovery time của provider điển hình; với concurrent load 200 req, đủ thời gian cho ≥1 chu kỳ open→half_open→closed |
| `circuit_breaker.success_threshold` | 1 | 1 probe thành công là đủ tin tưởng provider; threshold cao hơn làm tăng MTTR |
| `cache.ttl_seconds` | 300 | 5 phút tươi cho FAQ — đủ để tránh cache stale, đủ để thu được hit rate |
| `cache.similarity_threshold` | 0.92 | Test trước cho thấy 0.85 cho false hits "policy 2024" vs "policy 2026"; 0.92 với hybrid score đảm bảo chỉ near-exact match được hit (false-hit guard vẫn lớp phòng thủ thứ 2) |
| `cache.backend` | memory | Default; switch sang `redis` cho multi-instance deployment |
| `load_test.requests` | 200 | Mỗi scenario 200 requests → có đủ datapoints cho p95/p99 percentile và CB cycle |
| `load_test.concurrency` | 10 | Mô phỏng traffic thực; thấp hơn không bộc lộ race condition, cao hơn gây timeout giả |

## 3. SLO definitions

| SLI | SLO target | Actual | Met? |
|---|---|---:|---|
| Availability | ≥ 99% | 96.5% | ❌ (do `primary_flaky_50`) |
| Latency P95 | < 600 ms | 492.69 ms | ✅ |
| Fallback success rate | ≥ 90% | 93.84% | ✅ |
| Cache hit rate | ≥ 10% | 15.0% | ✅ |
| Recovery time | < 5000 ms | 2220 ms | ✅ |

Diễn giải SLO miss: availability 96.5% chủ yếu kéo xuống bởi `primary_flaky_50` (availability 92.5%) — đây là scenario chaos cố ý gây nửa số request fail trên primary. Với chỉ scenarios "production-realistic" (cache_on + all_healthy), availability vượt 99%. Trong sản xuất thực, primary không có 50% fail rate, nên SLO 99% khả thi.

## 4. Metrics tổng hợp

| Metric | Value |
|---|---:|
| total_requests | 1000 |
| availability | 0.965 |
| error_rate | 0.035 |
| latency_p50_ms | 271.50 |
| latency_p95_ms | 492.69 |
| latency_p99_ms | 538.09 |
| fallback_success_rate | 0.9384 |
| cache_hit_rate | 0.15 |
| circuit_open_count | 9 |
| recovery_time_ms | 2220.0 |
| estimated_cost | $0.3510 |
| estimated_cost_saved | $0.15 |

Dữ liệu raw có trong [reports/metrics.json](metrics.json).

## 5. Cache comparison (cache_off vs cache_on)

Trích từ `cache_comparison` block:

| Metric | Không cache | Có cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 288.24 | 0.31 | **−287.93** (−99.9%) |
| latency_p95_ms | 507.12 | 318.72 | **−188.40** (−37.2%) |
| estimated_cost | $0.07685 | $0.02141 | **−$0.05544** (−72.1%) |
| cache_hit_rate | 0.00 | 0.75 | +0.75 |
| availability | 0.955 | 0.99 | +0.035 |

Cache mang lại 99.9% giảm p50 latency (exact-match shortcut bỏ qua hoàn toàn provider call), 72% giảm cost. p95 không giảm nhiều vì 25% miss vẫn phải đợi provider — đây là điều bình thường cho cache layer.

### Ví dụ false-hit guardrail bắt được

```
query mới:      "refund policy for 2026"
matched_key:    "refund policy for 2024"
similarity:     0.75 (hybrid Jaccard) — vượt threshold 0.5
guard action:   _looks_like_false_hit phát hiện {2024} != {2026}
                → return (None, 0.75) thay vì trả answer cũ
                → false_hit_log có entry để audit
```

## 6. Redis shared cache

### Tại sao cần shared cache

In-memory cache đóng kín trong process của 1 gateway instance. Khi deploy ngang (3-5 instance đứng sau load balancer), mỗi instance giữ cache riêng:
- Cache hit rate thấp hơn N lần (cache cho cùng query bị duplicate N copy)
- Cost saving không tận dụng được — instance B không biết instance A đã trả lời query đó
- TTL drift giữa các instance, response có thể không nhất quán

`SharedRedisCache` đặt cache layer ra Redis chung; mọi instance đều đọc/ghi vào cùng namespace → một query được trả lời 1 lần, mọi instance đều hit.

### Evidence: hai instance đọc cùng dữ liệu

```text
Instance c1 set 2 entries.
Instance c2 gets entry 1: "Reliability = availability + correctness under loa..." (score=1.0)
Instance c2 gets entry 2: "A state machine: CLOSED -> OPEN -> HALF_OPEN..." (score=1.0)
False-hit guard test: refund 2026 -> cached=None, false_hit_log=1
```

`c1` và `c2` là 2 `SharedRedisCache` riêng biệt cùng trỏ vào `redis://localhost:6379/0` với prefix `rl:demo:`. `c2` đọc được tất cả entry `c1` set. False-hit guard cũng hoạt động đúng (refund 2026 không match refund 2024).

### Redis CLI output

```bash
$ docker compose exec -T redis redis-cli KEYS "rl:demo:*"
rl:demo:d669d9ed8ba4
rl:demo:47a18f375480
rl:demo:3169695a66ac

$ docker compose exec -T redis redis-cli HGETALL rl:demo:d669d9ed8ba4
query
what is reliability
response
Reliability = availability + correctness under load
```

3 key tồn tại với prefix; mỗi key là Redis Hash có field `query` (lưu nguyên văn để similarity scan) và `response` (giá trị cache). TTL được set qua `EXPIRE` — Redis tự động xoá khi hết hạn, không cần manual eviction.

### Graceful degradation

`SharedRedisCache.__init__` set `socket_connect_timeout=2` và `socket_timeout=2`. Mọi Redis call trong `get`/`set` đều bọc `try/except Exception` → trả `(None, 0.0)` hoặc no-op khi Redis lỗi. Test `test_redis_graceful_degradation_when_unreachable` xác minh: dùng URL `redis://nonexistent.invalid:6379/0`, set/get/ping đều không crash. Production: nếu Redis fail mid-traffic, gateway tự động hành xử như cache disabled — request vẫn đi qua CB + provider chain.

### So sánh latency in-memory vs Redis

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms (cache hit) | ~0.3 ms | ~1-2 ms | Redis round-trip qua localhost socket |
| Shared state across instances | ❌ | ✅ | Lợi ích chính của Redis |
| Persistence sau restart | ❌ | ✅ (AOF enabled) | `docker-compose.yml` mount volume |

In-memory nhanh hơn ~5×, nhưng Redis cho horizontal scaling — đánh đổi đáng giá trong sản xuất.

## 7. Chaos scenarios

| Scenario | Expected | Observed | Pass/Fail |
|---|---|---|:-:|
| `primary_timeout_100` | Primary fail 100% → CB open, fallback giải quyết ≥90% traffic | `circuit_open_count=3`, `fallback_success_rate=0.955`, `availability=0.955` | ✅ pass |
| `primary_flaky_50` | Primary fail 50% → CB oscillate, có recovery cycle | `circuit_open_count=3`, `recovery_time_ms=2198`, `fallback_success_rate=0.918` | ✅ pass |
| `all_healthy` | Cả 2 healthy → CB không trip, error rate ~0 | `circuit_open_count=0`, `error_rate=0.0`, `latency_p95=242ms` | ✅ pass |
| `cache_off` | Default providers, không cache → baseline cost/latency | `cache_hit_rate=0`, `latency_p50=288ms`, `estimated_cost=$0.077` | ✅ pass |
| `cache_on` | Default providers, in-memory cache → giảm cost & latency | `cache_hit_rate=0.75`, `latency_p50=0.31ms`, `estimated_cost=$0.021` | ✅ pass |

Pass/fail criteria được code hoá trong `chaos.evaluate_scenario()` (xem [src/reliability_lab/chaos.py](../src/reliability_lab/chaos.py)) — không phụ thuộc cảm tính.

## 8. Failure analysis

**Weakness còn lại**: circuit breaker state là per-process. Khi deploy 3 instance, instance A có thể đang OPEN cho `primary` (đã thấy 3 failure), nhưng instance B và C vẫn CLOSED và tiếp tục gọi `primary` — vô tình gây retry storm về phía provider đang gặp sự cố. Điều này làm giảm hiệu quả của CB ở scale.

**Cách khắc phục** (chưa làm trong lab này, đề xuất cho production):

1. **Redis-backed circuit state**: thay vì `self.failure_count`, dùng `INCR rl:cb:primary:fail_count` với `EXPIRE` để các instance chia sẻ state. `allow_request()` đọc state từ Redis. Trade-off: mỗi CB check là 1 Redis round-trip (~1ms) — cần cân nhắc với latency budget.
2. **Coordination via pub/sub**: instance đầu tiên trip CB publish event lên Redis pub/sub channel, các instance khác subscribe và tự open CB local. Giảm round-trip nhưng tăng độ phức tạp.
3. **Provider-side rate limiter**: thay vì rely 100% vào CB phía client, có thêm token bucket per-provider để hard cap số request/giây, kể cả khi tất cả instance đều mới boot.

**Weakness thứ hai**: cache key dựa trên md5 của query lowercase+strip; query có whitespace/punctuation khác nhau có thể hash khác → miss cùng nội dung. Trong production cần normalize prompt (lowercase, collapse whitespace, strip punctuation) trước khi hash.

## 9. Next steps (concrete)

1. **Redis-backed CB counters**: implement `RedisCircuitBreaker` subclass; thêm tests `test_shared_circuit_state.py` để verify 2 gateway instances thấy cùng state. Estimated effort: 4-6h.
2. **Prometheus export**: thêm `prometheus_client` dependency, export `agent_requests_total{route,provider}`, `agent_latency_seconds`, `cache_hits_total`, `circuit_state` — phục vụ Grafana dashboard. Estimated effort: 2-3h.
3. **Prompt normalizer**: tách module `prompt_normalize.py` chuẩn hoá whitespace/casing/punctuation trước khi hash key + similarity check, kèm test bộ corpus 50 cặp paraphrase. Estimated effort: 3-4h.

---

## Reproducibility

```bash
python -m pip install -e ".[dev]"
docker compose up -d
make test           # 36 passed (with Redis up)
make run-chaos      # generates reports/metrics.json with the structure above
make report         # generates reports/final_report.md from metrics
```
