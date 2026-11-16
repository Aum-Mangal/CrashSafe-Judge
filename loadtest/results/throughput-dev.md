# Throughput

Measured 2026-11-16 17:41 UTC on Lenv: 11th Gen Intel(R) Core(TM) i5-11320H @ 3.20GHz, 8 CPUs, k6 v2.4.0. 30 contestants submit with no pause between submissions (a correct answer, languages cpp,python in turn), so the queue is never empty and the workers set the pace. Each run lasts 180 s; the first 30 s are left out. k6 and the API run on the same machine as the workers. Times come from the database: verdict time is submission to result, queue wait is submission to a worker claiming it. Times in ms.

| Workers | Judged | Jobs/s | Verdict p50 | p95 | p99 | Queue wait p50 | p95 | Not AC | 429s |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 115 | 0.77 | 37654 | 51905 | 53260 | 36386 | 49480 | 0 | 0 |
| 2 | 210 | 1.40 | 20347 | 28577 | 30646 | 18947 | 27142 | 0 | 0 |
| 3 | 274 | 1.83 | 15702 | 19375 | 20182 | 14095 | 17323 | 0 | 0 |

**Judging time per job** (a worker claiming it to its result, median, ms), by language:

| Workers | Language | Jobs | Judging p50 |
|---|---|---|---|
| 1 | cpp | 59 | 1091 |
| 1 | python | 56 | 950 |
| 2 | cpp | 104 | 1400 |
| 2 | python | 106 | 1195 |
| 3 | cpp | 140 | 1639 |
| 3 | python | 136 | 1413 |
