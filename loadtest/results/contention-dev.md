# Where workers contend

Measured 2026-11-17 03:59 UTC on Lenv, 8 CPUs. N jobs judged at once, back to back, plus one probe (below), through `judge_tests` (the worker's own code, without the database), for 60 s after 10 s of warm-up. Each job is loadtest-sum's 5 tests, C++ and Python in turn. Times are medians in ms, for the whole `docker` command as the worker sees it.

| At once | Jobs/s | C++ job | Python job | create | copy file | compile | syntax check | test run | remove |
|---|---|---|---|---|---|---|---|---|---|
| 0 | 0.00 | - | - | - | - | - | - | - | - |
| 1 | 0.67 | 1653 | 1304 | 201 | 98 | 211 | 277 | 107 | 137 |
| 2 | 0.90 | 2513 | 1902 | 290 | 128 | 306 | 402 | 150 | 205 |
| 3 | 1.57 | 2131 | 1703 | 246 | 126 | 305 | 364 | 143 | 171 |
| 4 | 1.80 | 2441 | 1929 | 296 | 136 | 346 | 406 | 158 | 202 |
| 6 | 1.82 | 3346 | 2654 | 381 | 188 | 451 | 539 | 219 | 259 |

**The probe**: a 0.3 s computation (`loadtest/timing.py`'s `cpu`), run back to back in its own sandbox beside the N jobs. CPU and wall are what judge-runner measured, in ms. Against alone is the CPU median over the median with no jobs running. This is the number a time limit is compared with.

| At once | Runs | CPU median | p95 | max | Wall median | Against alone |
|---|---|---|---|---|---|---|
| 0 | 157 | 280 | 290 | 300 | 288 | 1.00x |
| 1 | 149 | 290 | 310 | 330 | 299 | 1.04x |
| 2 | 124 | 330 | 370 | 440 | 337 | 1.18x |
| 3 | 135 | 310 | 340 | 370 | 321 | 1.11x |
| 4 | 130 | 320 | 340 | 460 | 329 | 1.14x |
| 6 | 100 | 370 | 440 | 490 | 384 | 1.32x |

**CPU in use**, as CPUs kept busy on average (out of 8). "Not seen" is CPU used by processes that started and ended between two 0.2 s samples.

| At once | All | gvisor_sentry | docker CLI | dockerd | containerd | containerd-shim | benchmark (python) | systemd | dbus-daemon | Not seen |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 1.16 | 0.82 | 0.08 | 0.03 | 0.02 | 0.01 | 0.01 | 0.00 | 0.00 | 0.19 |
| 1 | 2.19 | 1.04 | 0.31 | 0.12 | 0.11 | 0.05 | 0.02 | 0.02 | 0.01 | 0.50 |
| 2 | 2.81 | 1.22 | 0.51 | 0.19 | 0.17 | 0.07 | 0.03 | 0.03 | 0.01 | 0.55 |
| 3 | 3.60 | 1.48 | 0.72 | 0.25 | 0.21 | 0.09 | 0.04 | 0.03 | 0.02 | 0.73 |
| 4 | 4.05 | 1.62 | 0.86 | 0.29 | 0.24 | 0.11 | 0.05 | 0.03 | 0.02 | 0.80 |
| 6 | 5.00 | 1.86 | 1.17 | 0.38 | 0.31 | 0.16 | 0.07 | 0.05 | 0.02 | 0.93 |

## Repeat at 0, 2 and 3, ten minutes later

The first run's row for 2 jobs was slower than its row for 3, so 0, 2 and 3 were run again with the same settings.

| At once | Jobs/s | C++ job | Python job | create | copy file | compile | syntax check | test run | remove |
|---|---|---|---|---|---|---|---|---|---|
| 0 | 0.00 | - | - | - | - | - | - | - | - |
| 2 | 1.07 | 2028 | 1615 | 234 | 119 | 281 | 356 | 134 | 161 |
| 3 | 1.53 | 2178 | 1729 | 260 | 125 | 299 | 371 | 142 | 175 |

**The probe**: a 0.3 s computation (`loadtest/timing.py`'s `cpu`), run back to back in its own sandbox beside the N jobs. CPU and wall are what judge-runner measured, in ms. Against alone is the CPU median over the median with no jobs running. This is the number a time limit is compared with.

| At once | Runs | CPU median | p95 | max | Wall median | Against alone |
|---|---|---|---|---|---|---|
| 0 | 145 | 300 | 330 | 350 | 305 | 1.00x |
| 2 | 134 | 310 | 350 | 400 | 319 | 1.03x |
| 3 | 134 | 310 | 340 | 360 | 322 | 1.03x |

**CPU in use**, as CPUs kept busy on average (out of 8). "Not seen" is CPU used by processes that started and ended between two 0.2 s samples.

| At once | All | gvisor_sentry | docker CLI | dockerd | containerd | containerd-shim | benchmark (python) | systemd | dbus-daemon | Not seen |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 1.12 | 0.81 | 0.08 | 0.03 | 0.02 | 0.01 | 0.01 | 0.00 | 0.00 | 0.16 |
| 2 | 2.89 | 1.27 | 0.53 | 0.20 | 0.18 | 0.07 | 0.03 | 0.03 | 0.01 | 0.56 |
| 3 | 3.54 | 1.48 | 0.70 | 0.25 | 0.22 | 0.09 | 0.04 | 0.03 | 0.02 | 0.69 |


## Earlier run without the probe

Before the probe was added, 03:50 UTC, the same settings at 1, 2, 3, 4 and 6 jobs. With no probe beside them, the jobs had one busy CPU fewer to share with.

| At once | Jobs/s | C++ job | Python job | create | copy file | compile | syntax check | test run | remove |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 0.65 | 1616 | 1282 | 200 | 95 | 214 | 269 | 107 | 136 |
| 2 | 1.23 | 1783 | 1404 | 210 | 102 | 230 | 302 | 112 | 148 |
| 3 | 1.65 | 2011 | 1595 | 239 | 115 | 277 | 337 | 129 | 165 |
| 4 | 1.97 | 2293 | 1803 | 266 | 131 | 317 | 389 | 148 | 185 |
| 6 | 2.30 | 2916 | 2347 | 353 | 164 | 414 | 481 | 187 | 233 |

**CPU in use**, as CPUs kept busy on average (out of 8). "Not seen" is CPU used by processes that started and ended between two 0.2 s samples.

| At once | All | gvisor_sentry | docker CLI | dockerd | containerd | containerd-shim | benchmark (python) | systemd | dbus-daemon | Not seen |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1.19 | 0.23 | 0.22 | 0.10 | 0.09 | 0.03 | 0.01 | 0.02 | 0.01 | 0.46 |
| 2 | 2.01 | 0.46 | 0.44 | 0.18 | 0.16 | 0.06 | 0.02 | 0.02 | 0.01 | 0.62 |
| 3 | 2.75 | 0.69 | 0.64 | 0.24 | 0.21 | 0.08 | 0.03 | 0.03 | 0.02 | 0.78 |
| 4 | 3.43 | 0.95 | 0.81 | 0.30 | 0.26 | 0.11 | 0.05 | 0.04 | 0.02 | 0.87 |
| 6 | 4.46 | 1.28 | 1.12 | 0.39 | 0.32 | 0.15 | 0.07 | 0.05 | 0.03 | 1.03 |
