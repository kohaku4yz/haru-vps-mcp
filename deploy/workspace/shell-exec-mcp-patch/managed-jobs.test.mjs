import assert from 'node:assert/strict';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {setTimeout as sleep} from 'node:timers/promises';
import {ManagedJobRegistry} from './managed-jobs.mjs';

async function withRegistry(options, fn) {
  const registry = new ManagedJobRegistry({startReaper: false, stopGraceMs: 30, groupWatchMs: 20, ...options});
  try { return await fn(registry); } finally {
    for (const job of [...registry.jobs.values()]) {
      if (job.state === 'running') await registry.stop(job.jobId);
    }
    registry.close();
  }
}

async function waitForFile(pathname, timeoutMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const text = (await readFile(pathname, 'utf8')).trim();
      if (text) return text;
    } catch {}
    await sleep(10);
  }
  throw new Error(`timed out waiting for ${pathname}`);
}

async function waitUntil(predicate, timeoutMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await sleep(10);
  }
  throw new Error('condition timeout');
}

async function processIsLive(pid) {
  try {
    const stat = await readFile(`/proc/${pid}/stat`, 'utf8');
    const end = stat.lastIndexOf(')');
    const state = stat.slice(end + 2).trim().split(/\s+/)[0];
    return state !== 'Z';
  } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
}

test('quick command returns completed result', async () => {
  await withRegistry({}, async (registry) => {
    const job = registry.start("printf 'ok'");
    const result = await registry.wait(job.jobId, 1000);
    assert.equal(result.running, false);
    assert.equal(result.exitCode, 0);
    assert.equal(result.stdout, 'ok');
  });
});

test('wait expiry returns a running job without killing it and result is retrievable', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'haru-job-'));
  const marker = path.join(dir, 'done');
  try {
    await withRegistry({}, async (registry) => {
      const job = registry.start(`sleep 0.2; printf done > ${JSON.stringify(marker)}`);
      const early = await registry.wait(job.jobId, 20);
      assert.equal(early.running, true);
      await sleep(300);
      const final = await registry.status(job.jobId);
      assert.equal(final.running, false);
      assert.equal(final.exitCode, 0);
      assert.equal(await readFile(marker, 'utf8'), 'done');
    });
  } finally { await rm(dir, {recursive: true, force: true}); }
});

test('explicit stop kills nested descendants in the owned process group', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'haru-tree-'));
  const marker = path.join(dir, 'late');
  try {
    await withRegistry({}, async (registry) => {
      const job = registry.start(`(sleep 0.4; printf late > ${JSON.stringify(marker)}) & wait`);
      await registry.wait(job.jobId, 20);
      const stopped = await registry.stop(job.jobId);
      assert.equal(stopped.state, 'stopped');
      await sleep(500);
      await assert.rejects(readFile(marker, 'utf8'));
    });
  } finally { await rm(dir, {recursive: true, force: true}); }
});

test('bounded collectors keep draining and noisy command still succeeds', async () => {
  await withRegistry({outputLimitBytes: 4096}, async (registry) => {
    const job = registry.start("python3 -c 'import sys; sys.stdout.write(\"x\" * 200000); sys.stderr.write(\"y\" * 200000)' ");
    const result = await registry.wait(job.jobId, 5000);
    assert.equal(result.exitCode, 0);
    assert.equal(result.stdout.length, 4096);
    assert.equal(result.stderr.length, 4096);
    assert.equal(result.stdoutTruncated, true);
    assert.equal(result.stderrTruncated, true);
    assert.ok(result.stdoutBytes >= 200000);
    assert.ok(result.stderrBytes >= 200000);
  });
});

test('completed unread jobs expire and registry saturation rejects without killing running jobs', async () => {
  let now = 0;
  await withRegistry({maxJobs: 2, completedTtlMs: 100, now: () => now}, async (registry) => {
    const a = registry.start('true');
    await registry.wait(a.jobId, 1000);
    const b = registry.start('sleep 10');
    await registry.wait(b.jobId, 1);
    assert.throws(() => registry.start('true'), /Job registry full/);
    assert.equal(registry.get(b.jobId).state, 'running');
    now = 101;
    const c = registry.start('true');
    assert.ok(c.jobId);
    assert.equal(registry.get(b.jobId).state, 'running');
  });
});

test('output activity and observation lease prevent idle reap', async () => {
  let now = 0;
  await withRegistry({idleLeaseMs: 100, busySampleWindowMs: 10, now: () => now, activityReader: async () => ({processCount: 1, cpuTicks: 0, readBytes: 0, writeBytes: 0})}, async (registry) => {
    const outputJob = registry.start('sleep 10');
    outputJob.lastOutputAtMs = 50;
    outputJob.lastObservedAtMs = 0;
    now = 120;
    await registry.reapOnce(now);
    assert.equal(outputJob.state, 'running');

    const observedJob = registry.start('sleep 10');
    observedJob.lastOutputAtMs = 0;
    observedJob.lastObservedAtMs = 50;
    now = 120;
    await registry.reapOnce(now);
    assert.equal(observedJob.state, 'running');
  });
});

test('quiet CPU or IO progress protects a job; stable inactivity becomes reapable', async () => {
  let now = 1000;
  let sample = {processCount: 1, cpuTicks: 10, readBytes: 20, writeBytes: 30};
  await withRegistry({idleLeaseMs: 100, busySampleWindowMs: 10, now: () => now, activityReader: async () => sample}, async (registry) => {
    const job = registry.start('sleep 10');
    job.lastOutputAtMs = 0;
    job.lastObservedAtMs = 0;
    await registry.reapOnce(now);
    assert.equal(job.state, 'running');

    now = 1020;
    sample = {...sample, cpuTicks: 11};
    await registry.reapOnce(now);
    assert.equal(job.state, 'running');

    now = 1040;
    await registry.reapOnce(now);
    assert.equal(job.state, 'reaped');
  });
});

test('status observation refreshes quiet job and persistent job is exempt from idle reap', async () => {
  let now = 1000;
  const activity = async () => ({processCount: 1, cpuTicks: 0, readBytes: 0, writeBytes: 0});
  await withRegistry({idleLeaseMs: 100, busySampleWindowMs: 10, now: () => now, activityReader: activity}, async (registry) => {
    const quiet = registry.start('sleep 10');
    quiet.lastOutputAtMs = 0;
    quiet.lastObservedAtMs = 0;
    await registry.status(quiet.jobId);
    assert.equal(quiet.lastObservedAtMs, 1000);
    now = 1050;
    await registry.reapOnce(now);
    assert.equal(quiet.state, 'running');

    const persistent = registry.start('sleep 10', {persistent: true});
    persistent.lastOutputAtMs = 0;
    persistent.lastObservedAtMs = 0;
    now = 5000;
    await registry.reapOnce(now);
    assert.equal(persistent.state, 'running');
    const stopped = await registry.stop(persistent.jobId);
    assert.equal(stopped.state, 'stopped');
  });
});

test('linux /proc sampling protects a real silent CPU-busy job', {skip: process.platform !== 'linux'}, async () => {
  await withRegistry({idleLeaseMs: 20, busySampleWindowMs: 10}, async (registry) => {
    const job = registry.start("node -e 'while (true) {}'");
    await registry.wait(job.jobId, 1);
    job.lastOutputAtMs = Date.now() - 1000;
    job.lastObservedAtMs = Date.now() - 1000;
    await registry.reapOnce();
    assert.equal(job.state, 'running');

    await sleep(60);
    job.lastOutputAtMs = Date.now() - 1000;
    job.lastObservedAtMs = Date.now() - 1000;
    await registry.reapOnce();
    assert.equal(job.state, 'running');
  });
});

test('launcher exit with inherited-pipe child stays owned and explicit stop kills the child', {skip: process.platform !== 'linux'}, async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'haru-launcher-pipe-'));
  const pidFile = path.join(dir, 'pid');
  try {
    await withRegistry({}, async (registry) => {
      const job = registry.start(`sleep 10 & echo $! > ${JSON.stringify(pidFile)}`);
      const childPid = Number(await waitForFile(pidFile));
      await waitUntil(() => job.launcherExited);
      assert.equal(job.state, 'running');
      assert.equal(await processIsLive(childPid), true);
      const stopped = await registry.stop(job.jobId);
      assert.equal(stopped.state, 'stopped');
      await waitUntil(async () => !(await processIsLive(childPid)));
    });
  } finally { await rm(dir, {recursive: true, force: true}); }
});

test('launcher exit with redirected child stays owned and explicit stop kills the child', {skip: process.platform !== 'linux'}, async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'haru-launcher-redir-'));
  const pidFile = path.join(dir, 'pid');
  try {
    await withRegistry({}, async (registry) => {
      const job = registry.start(`sleep 10 >/dev/null 2>&1 & echo $! > ${JSON.stringify(pidFile)}`);
      const childPid = Number(await waitForFile(pidFile));
      await waitUntil(() => job.launcherExited);
      await sleep(50);
      assert.equal(job.state, 'running');
      assert.equal(await processIsLive(childPid), true);
      const stopped = await registry.stop(job.jobId);
      assert.equal(stopped.state, 'stopped');
      await waitUntil(async () => !(await processIsLive(childPid)));
    });
  } finally { await rm(dir, {recursive: true, force: true}); }
});

test('launcher-exited same-group child is cleaned by idle reap', {skip: process.platform !== 'linux'}, async () => {
  let now = 1000;
  const dir = await mkdtemp(path.join(os.tmpdir(), 'haru-launcher-reap-'));
  const pidFile = path.join(dir, 'pid');
  try {
    await withRegistry({idleLeaseMs: 100, busySampleWindowMs: 10, now: () => now, activityReader: async () => ({processCount: 1, cpuTicks: 0, readBytes: 0, writeBytes: 0})}, async (registry) => {
      const job = registry.start(`sleep 10 >/dev/null 2>&1 & echo $! > ${JSON.stringify(pidFile)}`);
      const childPid = Number(await waitForFile(pidFile));
      await waitUntil(() => job.launcherExited);
      job.lastOutputAtMs = 0;
      job.lastObservedAtMs = 0;
      await registry.reapOnce(now);
      assert.equal(job.state, 'running');
      now = 1020;
      await registry.reapOnce(now);
      assert.equal(job.state, 'reaped');
      await waitUntil(async () => !(await processIsLive(childPid)));
    });
  } finally { await rm(dir, {recursive: true, force: true}); }
});

test('launcher-exited same-group child is cleaned by hard timeout', {skip: process.platform !== 'linux'}, async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'haru-launcher-hard-'));
  const pidFile = path.join(dir, 'pid');
  try {
    await withRegistry({}, async (registry) => {
      const job = registry.start(`sleep 10 >/dev/null 2>&1 & echo $! > ${JSON.stringify(pidFile)}`, {hardTimeoutMs: 100});
      const childPid = Number(await waitForFile(pidFile));
      await waitUntil(() => job.launcherExited);
      await waitUntil(() => job.state !== 'running', 1500);
      assert.equal(job.state, 'stopped');
      await waitUntil(async () => !(await processIsLive(childPid)));
    });
  } finally { await rm(dir, {recursive: true, force: true}); }
});

test('persistent launcher-exited child stays tracked and idle-exempt but explicit stop still works', {skip: process.platform !== 'linux'}, async () => {
  let now = 1000;
  const dir = await mkdtemp(path.join(os.tmpdir(), 'haru-launcher-persistent-'));
  const pidFile = path.join(dir, 'pid');
  try {
    await withRegistry({idleLeaseMs: 100, busySampleWindowMs: 10, now: () => now, activityReader: async () => ({processCount: 1, cpuTicks: 0, readBytes: 0, writeBytes: 0})}, async (registry) => {
      const job = registry.start(`sleep 10 >/dev/null 2>&1 & echo $! > ${JSON.stringify(pidFile)}`, {persistent: true});
      const childPid = Number(await waitForFile(pidFile));
      await waitUntil(() => job.launcherExited);
      job.lastOutputAtMs = 0;
      job.lastObservedAtMs = 0;
      now = 5000;
      await registry.reapOnce(now);
      assert.equal(job.state, 'running');
      assert.equal(await processIsLive(childPid), true);
      const stopped = await registry.stop(job.jobId);
      assert.equal(stopped.state, 'stopped');
      await waitUntil(async () => !(await processIsLive(childPid)));
    });
  } finally { await rm(dir, {recursive: true, force: true}); }
});

test('released ownership is terminal so a later numeric group reuse is never reacquired', async () => {
  let inspections = 0;
  await withRegistry({groupInspector: async () => {
    inspections += 1;
    return inspections === 1 ? {status: 'gone'} : {status: 'alive', activity: {processCount: 1, cpuTicks: 0, readBytes: 0, writeBytes: 0}};
  }}, async (registry) => {
    const job = registry.start('true');
    const result = await registry.wait(job.jobId, 1000);
    assert.equal(result.state, 'completed');
    assert.equal(job.ownershipReleased, true);
    const afterCompletionInspections = inspections;
    const stopped = await registry.stop(job.jobId);
    assert.equal(stopped.state, 'completed');
    assert.equal(inspections, afterCompletionInspections);
  });
});

test('setsid escape documents process-group best-effort boundary', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'haru-escape-'));
  const pidFile = path.join(dir, 'pid');
  try {
    await withRegistry({}, async (registry) => {
      const job = registry.start(`setsid sh -c 'echo $$ > ${pidFile}; sleep 10' & wait`);
      for (let i = 0; i < 100; i++) {
        try { if ((await readFile(pidFile, 'utf8')).trim()) break; } catch {}
        await sleep(10);
      }
      const escapedPid = Number((await readFile(pidFile, 'utf8')).trim());
      assert.ok(escapedPid > 1);
      await registry.stop(job.jobId);
      process.kill(escapedPid, 0);
      process.kill(escapedPid, 'SIGKILL');
    });
  } finally { await rm(dir, {recursive: true, force: true}); }
});
