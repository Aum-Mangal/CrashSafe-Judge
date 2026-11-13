# Sandbox image for C++ submissions.
FROM ubuntu:24.04

RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ \
 && rm -rf /var/lib/apt/lists/*

# Precompiled bits/stdc++.h. g++ only uses it when the compile flags match these exactly.
RUN header="$(find /usr/include -path '*/bits/stdc++.h' | head -n 1)" \
 && g++ -std=c++20 -O2 -x c++-header "$header" -o "$header.gch"
