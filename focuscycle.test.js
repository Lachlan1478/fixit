/**
 * focuscycle.test.js
 * Pure-function unit tests for the FocusCycle phase engine.
 * Run with: node focuscycle.test.js
 *
 * These functions are duplicated here (from the HTML file) so they can be
 * tested in Node.js without a build system.
 */

'use strict';

// ─────────────────────────────────────────────────────────────────
//  PHASE ENGINE (mirror of the code in focuscycle_for_deep_work_sprints.html)
// ─────────────────────────────────────────────────────────────────
const WORK_MS  = 25 * 60 * 1000;
const BREAK_MS =  5 * 60 * 1000;
const CYCLE_MS = WORK_MS + BREAK_MS;

function calcElapsed(sessionStart, totalPausedMs, pausedAt, now) {
  if (sessionStart === null) return 0;
  const ref = pausedAt !== null ? pausedAt : now;
  return Math.max(0, ref - sessionStart - totalPausedMs);
}

function getPhaseInfo(elapsed) {
  const e   = Math.max(0, elapsed);
  const pos = e % CYCLE_MS;
  const cyc = Math.floor(e / CYCLE_MS);

  if (pos < WORK_MS) {
    return { phase: 'work',  remaining: WORK_MS  - pos, progress: pos / WORK_MS,  cycleIndex: cyc };
  }
  const bp = pos - WORK_MS;
  return   { phase: 'break', remaining: BREAK_MS - bp,  progress: bp  / BREAK_MS, cycleIndex: cyc };
}

function nextPhaseBoundary(elapsed) {
  const e   = Math.max(0, elapsed);
  const pos = e % CYCLE_MS;
  if (pos < WORK_MS) return e - pos + WORK_MS;
  return e - pos + CYCLE_MS;
}

function fmtTime(ms) {
  const secs = Math.max(1, Math.ceil(ms / 1000));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

// Skip action (mirrors actSkip in the app)
// `now` must be passed explicitly so tests can control wall-clock time.
function applySkip(state, now) {
  const { sessionStart, totalPausedMs, pausedAt } = state;
  const ref     = pausedAt !== null ? pausedAt : now;
  const elapsed = calcElapsed(sessionStart, totalPausedMs, pausedAt, ref);
  const info    = getPhaseInfo(elapsed);
  return { ...state, sessionStart: sessionStart - info.remaining };
}

// ─────────────────────────────────────────────────────────────────
//  MINIMAL TEST RUNNER
// ─────────────────────────────────────────────────────────────────
let passed = 0, failed = 0;

function test(name, fn) {
  try {
    fn();
    console.log(`  ✅  ${name}`);
    passed++;
  } catch (err) {
    console.log(`  ❌  ${name}\n       → ${err.message}`);
    failed++;
  }
}

function eq(a, b, msg) {
  if (a !== b) throw new Error(`${msg ?? 'Expected equality'}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);
}

function near(a, b, tol = 0.0001, msg) {
  if (Math.abs(a - b) > tol) throw new Error(`${msg ?? 'Expected near-equal'}: got ${a}, want ~${b} (±${tol})`);
}

function ok(cond, msg) {
  if (!cond) throw new Error(msg ?? 'Assertion failed');
}

// ─────────────────────────────────────────────────────────────────
//  TESTS: calcElapsed
// ─────────────────────────────────────────────────────────────────
console.log('\n── calcElapsed ────────────────────────────────────────');

test('returns 0 when sessionStart is null', () => {
  eq(calcElapsed(null, 0, null, Date.now()), 0);
});

test('running: elapsed = now - sessionStart with no paused time', () => {
  const start = 1_000_000;
  eq(calcElapsed(start, 0, null, start + 10_000), 10_000);
});

test('running: elapsed accounts for totalPausedMs', () => {
  const start = 1_000_000;
  eq(calcElapsed(start, 3_000, null, start + 10_000), 7_000);
});

test('paused: elapsed is frozen at pausedAt, not at now', () => {
  const start    = 1_000_000;
  const pausedAt = start + 8_000;
  const frozenElapsed = calcElapsed(start, 0, pausedAt, start + 999_999);
  eq(frozenElapsed, 8_000, 'paused elapsed');
});

test('paused: accounts for totalPausedMs accumulated before pause', () => {
  const start    = 1_000_000;
  const pausedAt = start + 10_000;
  // 2 s was already spent paused before this pause
  eq(calcElapsed(start, 2_000, pausedAt, start + 999_999), 8_000);
});

test('clamps to 0 for negative inputs (clock adjustment edge case)', () => {
  eq(calcElapsed(1_000_100, 0, null, 1_000_000), 0);
});

// ─────────────────────────────────────────────────────────────────
//  TESTS: getPhaseInfo — phase detection
// ─────────────────────────────────────────────────────────────────
console.log('\n── getPhaseInfo — phase detection ─────────────────────');

test('elapsed=0 → work, remaining=25:00, cycle 0', () => {
  const i = getPhaseInfo(0);
  eq(i.phase, 'work');
  eq(i.remaining, WORK_MS);
  eq(i.cycleIndex, 0);
});

test('elapsed=1 ms → work, remaining=WORK_MS-1', () => {
  eq(getPhaseInfo(1).remaining, WORK_MS - 1);
});

test('elapsed=WORK_MS-1 → still work, remaining=1 ms', () => {
  const i = getPhaseInfo(WORK_MS - 1);
  eq(i.phase, 'work');
  eq(i.remaining, 1);
});

test('elapsed=WORK_MS → break starts, remaining=BREAK_MS, still cycle 0', () => {
  const i = getPhaseInfo(WORK_MS);
  eq(i.phase, 'break');
  eq(i.remaining, BREAK_MS);
  eq(i.cycleIndex, 0);
});

test('elapsed=WORK_MS+1 → break, remaining=BREAK_MS-1', () => {
  const i = getPhaseInfo(WORK_MS + 1);
  eq(i.phase, 'break');
  eq(i.remaining, BREAK_MS - 1);
});

test('elapsed=CYCLE_MS-1 → still break, remaining=1 ms', () => {
  const i = getPhaseInfo(CYCLE_MS - 1);
  eq(i.phase, 'break');
  eq(i.remaining, 1);
});

test('elapsed=CYCLE_MS → cycle 1 work starts', () => {
  const i = getPhaseInfo(CYCLE_MS);
  eq(i.phase, 'work');
  eq(i.remaining, WORK_MS);
  eq(i.cycleIndex, 1);
});

test('elapsed=CYCLE_MS+WORK_MS → cycle 1 break', () => {
  const i = getPhaseInfo(CYCLE_MS + WORK_MS);
  eq(i.phase, 'break');
  eq(i.cycleIndex, 1);
});

test('large elapsed — 10 full cycles still works (modulo)', () => {
  const i = getPhaseInfo(10 * CYCLE_MS + WORK_MS / 2);
  eq(i.phase, 'work');
  eq(i.cycleIndex, 10);
  near(i.remaining, WORK_MS / 2);
});

test('negative elapsed clamps to 0 → same as elapsed=0', () => {
  const i = getPhaseInfo(-5000);
  eq(i.phase, 'work');
  eq(i.remaining, WORK_MS);
});

// ─────────────────────────────────────────────────────────────────
//  TESTS: getPhaseInfo — progress
// ─────────────────────────────────────────────────────────────────
console.log('\n── getPhaseInfo — progress tracking ───────────────────');

test('progress=0 at work phase start', () => {
  near(getPhaseInfo(0).progress, 0);
});

test('progress=0.5 at work phase midpoint', () => {
  near(getPhaseInfo(WORK_MS / 2).progress, 0.5);
});

test('progress approaches 1 just before work phase ends', () => {
  ok(getPhaseInfo(WORK_MS - 1).progress > 0.9999, 'near 1');
});

test('progress=0 at break phase start', () => {
  near(getPhaseInfo(WORK_MS).progress, 0);
});

test('progress=0.5 at break phase midpoint', () => {
  near(getPhaseInfo(WORK_MS + BREAK_MS / 2).progress, 0.5);
});

// ─────────────────────────────────────────────────────────────────
//  TESTS: nextPhaseBoundary
// ─────────────────────────────────────────────────────────────────
console.log('\n── nextPhaseBoundary ───────────────────────────────────');

test('from work start → boundary = WORK_MS', () => {
  eq(nextPhaseBoundary(0), WORK_MS);
});

test('from work midpoint → boundary = WORK_MS', () => {
  eq(nextPhaseBoundary(WORK_MS / 2), WORK_MS);
});

test('from work-near-end → boundary = WORK_MS', () => {
  eq(nextPhaseBoundary(WORK_MS - 1), WORK_MS);
});

test('from break start → boundary = CYCLE_MS', () => {
  eq(nextPhaseBoundary(WORK_MS), CYCLE_MS);
});

test('from break midpoint → boundary = CYCLE_MS', () => {
  eq(nextPhaseBoundary(WORK_MS + BREAK_MS / 2), CYCLE_MS);
});

test('from cycle 2 work → boundary = CYCLE_MS + WORK_MS', () => {
  eq(nextPhaseBoundary(CYCLE_MS), CYCLE_MS + WORK_MS);
});

test('from cycle 2 break → boundary = 2*CYCLE_MS', () => {
  eq(nextPhaseBoundary(CYCLE_MS + WORK_MS + 1), 2 * CYCLE_MS);
});

// ─────────────────────────────────────────────────────────────────
//  TESTS: fmtTime display
// ─────────────────────────────────────────────────────────────────
console.log('\n── fmtTime display ─────────────────────────────────────');

test('25:00 at start of work', () => {
  eq(fmtTime(getPhaseInfo(0).remaining), '25:00');
});

test('05:00 at start of break', () => {
  eq(fmtTime(getPhaseInfo(WORK_MS).remaining), '05:00');
});

test('12:30 at 12.5 min into work', () => {
  eq(fmtTime(getPhaseInfo(12.5 * 60 * 1000).remaining), '12:30');
});

test('never shows 00:00 — final tick before transition shows 00:01', () => {
  eq(fmtTime(getPhaseInfo(WORK_MS - 1).remaining), '00:01');
});

test('immediately after work ends, shows 05:00 (no stall)', () => {
  eq(fmtTime(getPhaseInfo(WORK_MS).remaining), '05:00');
});

test('floor partial second up to 01 (1 ms remaining → 00:01)', () => {
  eq(fmtTime(1), '00:01');
});

test('leading zero on minutes below 10', () => {
  ok(fmtTime(9 * 60 * 1000).startsWith('09:'), 'starts with 09:');
});

// ─────────────────────────────────────────────────────────────────
//  TESTS: Skip action
// ─────────────────────────────────────────────────────────────────
console.log('\n── Skip action ─────────────────────────────────────────');

// Use a realistic epoch so Date.now() is never accidentally mixed in.
const EPOCH = 1_700_000_000_000; // Nov 2023 — safe reference point for tests

test('skip from work (running) → lands at break start', () => {
  const T0  = EPOCH;
  const now = T0 + WORK_MS / 2;          // 12.5 min into work
  const state = { sessionStart: T0, totalPausedMs: 0, pausedAt: null };
  const after = applySkip(state, now);
  const elapsed = calcElapsed(after.sessionStart, after.totalPausedMs, after.pausedAt, now);
  const info = getPhaseInfo(elapsed);
  eq(info.phase, 'break');
  near(info.remaining, BREAK_MS, 2);
});

test('skip from work (paused) → frozen elapsed at break boundary', () => {
  const T0       = EPOCH;
  const pausedAt = T0 + WORK_MS / 3;     // paused 8.3 min into work
  const state    = { sessionStart: T0, totalPausedMs: 0, pausedAt };
  const after    = applySkip(state, pausedAt); // now = pausedAt for paused skip
  const elapsed  = calcElapsed(after.sessionStart, after.totalPausedMs, after.pausedAt, T0 + 999_999);
  const info = getPhaseInfo(elapsed);
  eq(info.phase, 'break');
  near(info.remaining, BREAK_MS, 2);
});

test('skip from break (running) → lands at next work start', () => {
  const T0  = EPOCH;
  const now = T0 + WORK_MS + BREAK_MS / 2;  // 2.5 min into break
  const state = { sessionStart: T0, totalPausedMs: 0, pausedAt: null };
  const after = applySkip(state, now);
  const elapsed = calcElapsed(after.sessionStart, after.totalPausedMs, after.pausedAt, now);
  const info = getPhaseInfo(elapsed);
  eq(info.phase, 'work');
  near(info.remaining, WORK_MS, 2);
});

test('double-skip from work → ends up at next work phase', () => {
  const T0  = EPOCH;
  const now = T0 + WORK_MS / 4;            // 6.25 min into work
  const s0  = { sessionStart: T0, totalPausedMs: 0, pausedAt: null };
  const s1  = applySkip(s0, now);           // → break
  const s2  = applySkip(s1, now);           // → next work
  const e   = calcElapsed(s2.sessionStart, s2.totalPausedMs, s2.pausedAt, now);
  const info = getPhaseInfo(e);
  eq(info.phase, 'work');
  near(info.remaining, WORK_MS, 2);
});

test('skip from work near end (1 ms remaining) → break with full BREAK_MS', () => {
  const T0  = EPOCH;
  const now = T0 + WORK_MS - 1;            // 1 ms before work ends
  const state = { sessionStart: T0, totalPausedMs: 0, pausedAt: null };
  const after = applySkip(state, now);
  const e   = calcElapsed(after.sessionStart, after.totalPausedMs, after.pausedAt, now);
  const info = getPhaseInfo(e);
  eq(info.phase, 'break');
  near(info.remaining, BREAK_MS, 2);
});

// ─────────────────────────────────────────────────────────────────
//  TESTS: Pause / Resume mechanics
// ─────────────────────────────────────────────────────────────────
console.log('\n── Pause / Resume mechanics ────────────────────────────');

test('elapsed is frozen while paused regardless of how much time passes', () => {
  const T0    = 1_000_000;
  const run10 = T0 + 10_000;       // paused 10 s into session
  const later = T0 + 999_999;      // called way later
  const e1 = calcElapsed(T0, 0, run10, run10 + 1);
  const e2 = calcElapsed(T0, 0, run10, later);
  eq(e1, e2, 'frozen elapsed');
});

test('resuming after pause: total paused gap is absorbed', () => {
  const T0        = 1_000_000;
  const pausedAt  = T0 + 5_000;    // paused at 5 s
  const resumedAt = T0 + 8_000;    // resumed at 8 s → 3 s paused
  const newTotalPausedMs = 0 + (resumedAt - pausedAt); // = 3 000

  const nowLater = T0 + 15_000;
  const elapsed = calcElapsed(T0, newTotalPausedMs, null, nowLater);
  // Effective run time = 5 s (before pause) + 7 s (after resume) = 12 s
  eq(elapsed, 12_000);
});

test('multiple pause/resume cycles accumulate correctly', () => {
  const T0 = 1_000_000;
  let totalPaused = 0;
  let start = T0;

  // Run 5 s, pause 2 s, run 5 s, pause 3 s, then check at now = T0 + 20 s
  const pausedAt1 = T0 + 5_000;
  totalPaused += 2_000;
  const pausedAt2 = T0 + 12_000;  // resumed at 7 s, ran 5 more
  totalPaused += 3_000;
  // resumed at 15 s, currently running at 20 s
  const now = T0 + 20_000;
  // effective elapsed = (20 - 0) - (2 + 3) = 15 s
  eq(calcElapsed(T0, totalPaused, null, now), 15_000);
});

// ─────────────────────────────────────────────────────────────────
//  TESTS: State serialisation round-trip
// ─────────────────────────────────────────────────────────────────
console.log('\n── State serialisation round-trip ──────────────────────');

test('serialised + deserialised state produces same elapsed', () => {
  const T0  = 1_000_000;
  const now = T0 + 7_000;
  const original = { sessionStart: T0, totalPausedMs: 1_000, pausedAt: null, isRunning: true };
  const restored  = JSON.parse(JSON.stringify(original));
  eq(
    calcElapsed(original.sessionStart,  original.totalPausedMs,  original.pausedAt,  now),
    calcElapsed(restored.sessionStart,  restored.totalPausedMs,  restored.pausedAt,  now),
  );
});

test('serialised paused state produces same frozen elapsed after restore', () => {
  const T0 = 1_000_000;
  const s  = { sessionStart: T0, totalPausedMs: 0, pausedAt: T0 + 12_000 };
  const r  = JSON.parse(JSON.stringify(s));
  const farFuture = T0 + 999_999;
  eq(
    calcElapsed(s.sessionStart, s.totalPausedMs, s.pausedAt, farFuture),
    calcElapsed(r.sessionStart, r.totalPausedMs, r.pausedAt, farFuture),
  );
});

// ─────────────────────────────────────────────────────────────────
//  RESULTS
// ─────────────────────────────────────────────────────────────────
console.log(`\n${'─'.repeat(57)}`);
const icon  = failed === 0 ? '✅' : '❌';
const total = passed + failed;
console.log(`${icon}  ${passed}/${total} passed${failed > 0 ? `, ${failed} FAILED` : ''}`);
console.log('');

process.exit(failed > 0 ? 1 : 0);
