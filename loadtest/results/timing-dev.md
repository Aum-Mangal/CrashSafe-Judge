# Fair timing

Measured 2026-11-16 14:33 UTC on Lenv: 11th Gen Intel(R) Core(TM) i5-11320H @ 3.20GHz, kernel 6.18.58.1-microsoft-standard-WSL2, 29.9.3 docker, runsc version release-20261102.0. Each program ran 50 times under each runtime, alternating, through judge-runner with the judge's own sandbox flags. Times are in ms.

CPU and wall are what judge-runner measured for the program alone. Round trip is the whole `docker exec` as the worker sees it, runner included. Spread is the standard deviation as a share of the median.

| Program | Runtime | CPU median | p95 | range | spread | Wall median | p95 | range | spread | Round trip median | p95 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cpu: pure computation | runsc | 290 | 300 | 280 to 330 | 3.0% | 299 | 316 | 287 to 341 | 3.0% | 390 | 411 |
| cpu: pure computation | runc | 280 | 293 | 275 to 302 | 1.9% | 278 | 297 | 274 to 302 | 2.4% | 383 | 411 |
| syscalls: 200 000 tiny system calls | runsc | 280 | 350 | 230 to 400 | 12.3% | 298 | 410 | 269 to 430 | 13.7% | 390 | 528 |
| syscalls: 200 000 tiny system calls | runc | 21 | 25 | 18 to 41 | 16.1% | 23 | 26 | 21 to 42 | 13.3% | 132 | 191 |
| memory: touching 256 MB | runsc | 120 | 160 | 110 to 200 | 15.2% | 121 | 166 | 113 to 210 | 15.7% | 210 | 279 |
| memory: touching 256 MB | runc | 71 | 89 | 66 to 154 | 21.3% | 71 | 88 | 66 to 149 | 20.5% | 168 | 220 |
| python: Python start-up and a short loop | runsc | 60 | 110 | 50 to 160 | 31.5% | 64 | 95 | 58 to 165 | 28.0% | 158 | 259 |
| python: Python start-up and a short loop | runc | 54 | 81 | 50 to 130 | 27.1% | 54 | 86 | 48 to 130 | 28.4% | 156 | 282 |
| empty: the fixed cost of one run | runsc | 10 | 20 | 0 to 20 | 54.3% | 5 | 9 | 3 to 14 | 42.2% | 113 | 180 |
| empty: the fixed cost of one run | runc | 0 | 1 | 0 to 1 | 0.0% | 1 | 2 | 0 to 2 | 42.0% | 120 | 182 |

**gVisor's cost** (runsc median over runc median):

- cpu: CPU 1.04x, wall 1.07x, round trip 1.02x
- syscalls: CPU 13.33x, wall 12.98x, round trip 2.96x
- memory: CPU 1.69x, wall 1.70x, round trip 1.25x
- python: CPU 1.11x, wall 1.21x, round trip 1.01x
- empty: CPU n/a, wall 5.00x, round trip 0.95x

**Starting a sandbox** (`docker run -d`, until Docker returns):

- runsc: median 222 ms, p95 287 ms, 20 starts
- runc: median 220 ms, p95 260 ms, 20 starts
