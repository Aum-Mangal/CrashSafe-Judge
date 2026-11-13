# Sandbox images. Build from the repo root (scripts/build-images.sh does this):
#   docker build --target cpp -t judge-cpp -f deploy/images/Dockerfile .
#   docker build --target python -t judge-python -f deploy/images/Dockerfile .

# judge-runner, built once and copied into every sandbox image
FROM ubuntu:24.04 AS runner
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*
COPY runner/judge_runner.c /src/judge_runner.c
RUN gcc -O2 -static -Wall -Wextra -Werror -o /judge-runner /src/judge_runner.c

FROM ubuntu:24.04 AS cpp
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ \
 && rm -rf /var/lib/apt/lists/*
# Precompiled bits/stdc++.h. g++ only uses it when the compile flags match these exactly.
RUN header="$(find /usr/include -path '*/bits/stdc++.h' | head -n 1)" \
 && g++ -std=c++20 -O2 -x c++-header "$header" -o "$header.gch"
COPY --from=runner /judge-runner /usr/local/bin/judge-runner

FROM python:3.12-slim AS python
COPY --from=runner /judge-runner /usr/local/bin/judge-runner
