// Throughput test (ARCHITECTURE.md §13): contestants submitting through the real REST API.
//
// Each virtual user (VU) is one contestant who submits, polls until the verdict arrives, and
// submits again at once. With no pause between submissions the queue never empties, so the
// workers decide the pace: this measures the most the judge can handle (a saturation test).
// throughput.sh starts it once per worker count and reads the exact numbers from the database.
//
//   k6 run -e BASE_URL=http://127.0.0.1:8000 -e SETUP=tokens.json -e VUS=30 -e DURATION=3m throughput.js

import http from 'k6/http';
import { check, sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const SETUP = JSON.parse(open(__ENV.SETUP)); // written by loadtest/prepare.py
const LANGUAGES = (__ENV.LANGUAGES || 'cpp,python').split(',');
const SOURCES = { cpp: open('./solutions/sum.cpp'), python: open('./solutions/sum.py') };
const POLL_SECONDS = 0.5;
const GIVE_UP_MS = 5 * 60 * 1000;

const judged = new Counter('jobs_judged');
const accepted = new Rate('verdict_ac'); // every answer is right, so anything else is a bug
const rejected = new Counter('rejected_429');
const verdictTime = new Trend('submit_to_verdict', true);

export const options = {
  scenarios: {
    contestants: {
      executor: 'constant-vus',
      vus: Number(__ENV.VUS || 30),
      duration: __ENV.DURATION || '3m',
      gracefulStop: '60s', // lets a contestant waiting at the end see their verdict
    },
  },
  thresholds: {
    verdict_ac: ['rate==1'],
    http_req_failed: ['rate<0.01'],
  },
};

// A 429 is the API pushing back as designed (rate limit, inflight or queue cap), not an error
http.setResponseCallback(http.expectedStatuses(200, 201, 429));

export default function () {
  const vu = exec.vu.idInTest;
  if (vu > SETUP.tokens.length) throw new Error(`only ${SETUP.tokens.length} accounts for ${vu} VUs`);
  const headers = { Authorization: `Bearer ${SETUP.tokens[vu - 1]}`, 'Content-Type': 'application/json' };
  const language = LANGUAGES[(vu + exec.vu.iterationInScenario) % LANGUAGES.length];
  const tags = { language };

  const started = Date.now();
  const res = http.post(`${BASE}/submissions`,
    JSON.stringify({ problem: SETUP.problem, language, source: SOURCES[language] }),
    { headers, tags: { name: 'submit', language } });
  if (res.status === 429) {
    rejected.add(1, tags);
    sleep(Number(res.headers['Retry-After'] || 1));
    return;
  }
  if (!check(res, { 'submission accepted': (r) => r.status === 201 })) return;
  const id = res.json('id');

  while (Date.now() - started < GIVE_UP_MS) {
    sleep(POLL_SECONDS);
    const r = http.get(`${BASE}/submissions/${id}`, { headers, tags: { name: 'poll', language } });
    if (!check(r, { 'poll answered': (x) => x.status === 200 })) return;
    const status = r.json('status');
    if (status === 'queued' || status === 'running') continue;
    verdictTime.add(Date.now() - started, tags);
    judged.add(1, tags);
    accepted.add(status === 'completed' && r.json('verdict') === 'AC', tags);
    return;
  }
  accepted.add(false, tags);
}
