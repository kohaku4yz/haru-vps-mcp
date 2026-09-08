import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {readdir, readFile} from 'node:fs/promises';

const DEFAULT_OUTPUT_LIMIT_BYTES = 1024 * 1024;
const DEFAULT_MAX_JOBS = 64;
const DEFAULT_IDLE_LEASE_MS = 60 * 60 * 1000;
const DEFAULT_SWEEP_MS = 5 * 60 * 1000;
const DEFAULT_COMPLETED_TTL_MS = 60 * 60 * 1000;
const DEFAULT_STOP_GRACE_MS = 1000;
const DEFAULT_BUSY_SAMPLE_WINDOW_MS = 60 * 1000;
const DEFAULT_GROUP_WATCH_MS = 250;

function positiveInt(value, fallback) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function nonNegativeInt(value, fallback) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : fallback;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class BoundedCollector {
  constructor(limitBytes = DEFAULT_OUTPUT_LIMIT_BYTES) {
    this.limitBytes = positiveInt(limitBytes, DEFAULT_OUTPUT_LIMIT_BYTES);
    this.chunks = [];
    this.retainedBytes = 0;
    this.totalBytes = 0;
    this.truncated = false;
  }

  append(data) {
    const buffer = Buffer.isBuffer(data) ? data : Buffer.from(data);
    this.totalBytes += buffer.length;
    if (this.retainedBytes >= this.limitBytes) {
      this.truncated = true;
      return;
    }
    const remaining = this.limitBytes - this.retainedBytes;
    const kept = buffer.length <= remaining ? buffer : buffer.subarray(0, remaining);
    if (kept.length) {
      this.chunks.push(Buffer.from(kept));
      this.retainedBytes += kept.length;
    }
    if (kept.length < buffer.length) this.truncated = true;
  }

  text() {
    return Buffer.concat(this.chunks, this.retainedBytes).toString('utf8');
  }
}

function parseProcStat(text) {
  const end = text.lastIndexOf(')');
  if (end < 0) return null;
  const rest = text.slice(end + 2).trim().split(/\s+/);
  if (rest.length < 20) return null;
  const state = rest[0];
  const pgrp = Number(rest[2]);
  const session = Number(rest[3]);
  const utime = Number(rest[11]);
  const stime = Number(rest[12]);
  const startTimeTicks = Number(rest[19]);
  if (![pgrp, session, utime, stime, startTimeTicks].every(Number.isFinite)) return null;
  return {state, pgrp, session, cpuTicks: utime + stime, startTimeTicks};
}

function parseProcIo(text) {
  const values = {};
  for (const line of text.split('\n')) {
    const match = /^(rchar|wchar|read_bytes|write_bytes):\s*(\d+)$/.exec(line.trim());
    if (match) values[match[1]] = Number(match[2]);
  }
  return {
    readBytes: (values.rchar ?? 0) + (values.read_bytes ?? 0),
    writeBytes: (values.wchar ?? 0) + (values.write_bytes ?? 0),
  };
}

export async function inspectProcessGroupActivity(pgid, sessionId = pgid) {
  if (process.platform !== 'linux' || !Number.isInteger(pgid) || pgid <= 0 || !Number.isInteger(sessionId) || sessionId <= 0) {
    return {status: 'unsupported'};
  }
  let entries;
  try {
    entries = await readdir('/proc', {withFileTypes: true});
  } catch {
    return {status: 'unknown'};
  }
  let processCount = 0;
  let cpuTicks = 0;
  let readBytes = 0;
  let writeBytes = 0;
  const members = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue;
    try {
      const stat = parseProcStat(await readFile(`/proc/${entry.name}/stat`, 'utf8'));
      if (!stat || stat.state === 'Z' || stat.pgrp !== pgid || stat.session !== sessionId) continue;
      processCount += 1;
      cpuTicks += stat.cpuTicks;
      members.push({pid: Number(entry.name), startTimeTicks: stat.startTimeTicks});
      try {
        const io = parseProcIo(await readFile(`/proc/${entry.name}/io`, 'utf8'));
        readBytes += io.readBytes;
        writeBytes += io.writeBytes;
      } catch {
        // CPU progress remains useful when /proc/<pid>/io is unavailable.
      }
    } catch {
      // Process may have exited while /proc was being sampled.
    }
  }
  if (processCount === 0) return {status: 'gone'};
  return {
    status: 'alive',
    activity: {processCount, cpuTicks, readBytes, writeBytes},
    members,
  };
}

export async function sampleProcessGroupActivity(pgid, sessionId = pgid) {
  const inspected = await inspectProcessGroupActivity(pgid, sessionId);
  return inspected.status === 'alive' ? inspected.activity : null;
}

function activityChanged(before, after) {
  return before.processCount !== after.processCount || before.cpuTicks !== after.cpuTicks || before.readBytes !== after.readBytes || before.writeBytes !== after.writeBytes;
}

function iso(ms) {
  return ms === null ? null : new Date(ms).toISOString();
}

export class ManagedJobRegistry {
  constructor(options = {}) {
    this.outputLimitBytes = positiveInt(options.outputLimitBytes ?? process.env.SHELL_EXEC_MCP_OUTPUT_LIMIT_BYTES, DEFAULT_OUTPUT_LIMIT_BYTES);
    this.maxJobs = positiveInt(options.maxJobs ?? process.env.SHELL_EXEC_MCP_MAX_JOBS, DEFAULT_MAX_JOBS);
    this.idleLeaseMs = positiveInt(options.idleLeaseMs ?? process.env.SHELL_EXEC_MCP_IDLE_LEASE_MS, DEFAULT_IDLE_LEASE_MS);
    this.sweepMs = positiveInt(options.sweepMs ?? process.env.SHELL_EXEC_MCP_REAPER_SWEEP_MS, DEFAULT_SWEEP_MS);
    this.completedTtlMs = positiveInt(options.completedTtlMs ?? process.env.SHELL_EXEC_MCP_COMPLETED_TTL_MS, DEFAULT_COMPLETED_TTL_MS);
    this.stopGraceMs = nonNegativeInt(options.stopGraceMs ?? process.env.SHELL_EXEC_MCP_STOP_GRACE_MS, DEFAULT_STOP_GRACE_MS);
    this.busySampleWindowMs = nonNegativeInt(options.busySampleWindowMs ?? process.env.SHELL_EXEC_MCP_BUSY_SAMPLE_WINDOW_MS, DEFAULT_BUSY_SAMPLE_WINDOW_MS);
    this.groupWatchMs = positiveInt(options.groupWatchMs, DEFAULT_GROUP_WATCH_MS);
    this.groupInspector = options.groupInspector ?? inspectProcessGroupActivity;
    this.activityReader = options.activityReader ?? null;
    this.now = options.now ?? (() => Date.now());
    this.jobs = new Map();
    this.timer = options.startReaper === false ? null : setInterval(() => {
      this.reapOnce().catch((error) => console.error('[shell-exec-mcp] job reaper error:', error));
    }, this.sweepMs);
    this.timer?.unref?.();
  }

  close() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    for (const job of this.jobs.values()) this.clearGroupWatch(job);
  }

  resetIdleEvidence(job) {
    job.activitySnapshot = null;
    job.activitySampleAtMs = null;
  }

  clearGroupWatch(job) {
    if (job.groupWatchTimer) clearTimeout(job.groupWatchTimer);
    job.groupWatchTimer = null;
  }

  clearJobTimers(job) {
    if (job.hardTimer) clearTimeout(job.hardTimer);
    job.hardTimer = null;
    this.clearGroupWatch(job);
  }

  resolveJob(job) {
    if (job.doneResolved) return;
    job.doneResolved = true;
    job.resolveDone();
  }

  purgeExpiredCompleted(now = this.now()) {
    for (const [jobId, job] of this.jobs) {
      if (job.state !== 'running' && job.completedAtMs !== null && now - job.completedAtMs >= this.completedTtlMs) {
        this.jobs.delete(jobId);
      }
    }
  }

  ensureCapacity() {
    this.purgeExpiredCompleted();
    if (this.jobs.size >= this.maxJobs) {
      throw new Error(`Job registry full (${this.maxJobs}); wait for completed jobs to expire or stop/inspect existing jobs`);
    }
  }

  releaseCompleted(jobId) {
    const job = this.jobs.get(jobId);
    if (job && job.state !== 'running') this.jobs.delete(jobId);
  }

  start(command, {persistent = false, hardTimeoutMs = null} = {}) {
    this.ensureCapacity();
    const startedAtMs = this.now();
    const child = spawn('bash', ['-c', command], {
      stdio: ['ignore', 'pipe', 'pipe'],
      detached: process.platform !== 'win32',
    });
    const jobId = randomUUID();
    let resolveDone;
    const done = new Promise((resolve) => { resolveDone = resolve; });
    const job = {
      jobId,
      command,
      process: child,
      pgid: child.pid ?? null,
      sessionId: child.pid ?? null,
      persistent: Boolean(persistent),
      state: 'running',
      exitCode: null,
      signal: null,
      stopReason: null,
      launcherExited: false,
      launcherExitCode: null,
      launcherSignal: null,
      ownershipReleased: false,
      startedAtMs,
      lastOutputAtMs: startedAtMs,
      lastObservedAtMs: startedAtMs,
      completedAtMs: null,
      stdout: new BoundedCollector(this.outputLimitBytes),
      stderr: new BoundedCollector(this.outputLimitBytes),
      activitySnapshot: null,
      activitySampleAtMs: null,
      done,
      resolveDone,
      doneResolved: false,
      hardTimer: null,
      groupWatchTimer: null,
    };
    this.jobs.set(jobId, job);

    child.stdout?.on('data', (data) => {
      job.stdout.append(data);
      job.lastOutputAtMs = this.now();
      this.resetIdleEvidence(job);
    });
    child.stderr?.on('data', (data) => {
      job.stderr.append(data);
      job.lastOutputAtMs = this.now();
      this.resetIdleEvidence(job);
    });
    child.once('error', (error) => {
      job.stderr.append(`\nProcess error: ${error.message}`);
      if (job.state === 'running') {
        job.state = 'failed';
        job.exitCode = 1;
        job.ownershipReleased = true;
        job.completedAtMs = this.now();
        this.clearJobTimers(job);
        this.resolveJob(job);
      }
    });
    child.once('exit', (code, signal) => {
      job.launcherExited = true;
      job.launcherExitCode = code;
      job.launcherSignal = signal;
      this.refreshAfterLauncherExit(job).catch((error) => console.error('[shell-exec-mcp] launcher-exit ownership check error:', error));
    });

    const hard = Number(hardTimeoutMs);
    if (Number.isInteger(hard) && hard > 0) {
      job.hardTimer = setTimeout(() => {
        this.stop(jobId, 'hard_timeout').catch((error) => console.error('[shell-exec-mcp] hard timeout cleanup error:', error));
      }, hard);
      job.hardTimer.unref?.();
    }
    return job;
  }

  get(jobId, {observe = false} = {}) {
    const job = this.jobs.get(jobId);
    if (!job) throw new Error(`Job not found: ${jobId}`);
    if (observe && job.state === 'running') {
      job.lastObservedAtMs = this.now();
      this.resetIdleEvidence(job);
    }
    return job;
  }

  snapshot(job, {observe = false} = {}) {
    if (observe && job.state === 'running') {
      job.lastObservedAtMs = this.now();
      this.resetIdleEvidence(job);
    }
    return {
      jobId: job.jobId,
      running: job.state === 'running',
      state: job.state,
      persistent: job.persistent,
      stdout: job.stdout.text(),
      stderr: job.stderr.text(),
      stdoutTruncated: job.stdout.truncated,
      stderrTruncated: job.stderr.truncated,
      stdoutBytes: job.stdout.totalBytes,
      stderrBytes: job.stderr.totalBytes,
      exitCode: job.exitCode,
      signal: job.signal,
      startedAt: iso(job.startedAtMs),
      lastOutputAt: iso(job.lastOutputAtMs),
      lastObservedAt: iso(job.lastObservedAtMs),
      completedAt: iso(job.completedAtMs),
    };
  }

  async inspectOwnedGroup(job) {
    if (job.ownershipReleased) return {status: 'gone'};
    if (job.pgid == null || job.sessionId == null) return {status: job.launcherExited ? 'gone' : 'unknown'};
    if (process.platform !== 'linux') {
      return job.process.exitCode == null ? {status: 'alive'} : {status: 'gone'};
    }
    let inspected;
    try {
      inspected = await this.groupInspector(job.pgid, job.sessionId);
    } catch {
      return {status: 'unknown'};
    }
    if (!inspected || !['alive', 'gone'].includes(inspected.status)) return {status: 'unknown'};
    if (inspected.status === 'gone') {
      if (!job.launcherExited) return {status: 'unknown'};
      // detached=true makes the launcher both session and process-group leader.
      // Once no live member remains in that exact session/group, release ownership
      // permanently so a later numeric PGID reuse can never be signalled by this job.
      job.ownershipReleased = true;
    }
    return inspected;
  }

  finalizeNaturalCompletion(job) {
    if (job.state !== 'running' || job.stopReason !== null) return;
    job.ownershipReleased = true;
    job.state = 'completed';
    job.exitCode = job.launcherExitCode ?? 1;
    job.signal = job.launcherSignal;
    job.completedAtMs = this.now();
    this.clearJobTimers(job);
    this.resolveJob(job);
  }

  finalizeStopped(job, reason) {
    if (job.state !== 'running') return;
    job.ownershipReleased = true;
    job.state = reason === 'reaped' ? 'reaped' : 'stopped';
    job.exitCode = job.launcherExitCode;
    job.signal = job.launcherSignal;
    job.completedAtMs = this.now();
    this.clearJobTimers(job);
    this.resolveJob(job);
  }

  scheduleGroupWatch(job) {
    if (job.state !== 'running' || !job.launcherExited || job.groupWatchTimer) return;
    job.groupWatchTimer = setTimeout(() => {
      job.groupWatchTimer = null;
      this.refreshAfterLauncherExit(job).catch((error) => console.error('[shell-exec-mcp] group ownership watch error:', error));
    }, this.groupWatchMs);
    job.groupWatchTimer.unref?.();
  }

  async refreshAfterLauncherExit(job) {
    if (job.state !== 'running' || !job.launcherExited) return;
    const inspected = await this.inspectOwnedGroup(job);
    if (inspected.status === 'gone') {
      if (job.stopReason === null) this.finalizeNaturalCompletion(job);
      return;
    }
    this.scheduleGroupWatch(job);
  }

  async status(jobId) {
    const job = this.get(jobId);
    if (job.state === 'running' && job.launcherExited) await this.refreshAfterLauncherExit(job);
    if (job.state === 'running') {
      job.lastObservedAtMs = this.now();
      this.resetIdleEvidence(job);
    }
    return this.snapshot(job);
  }

  async wait(jobId, waitMs) {
    const job = this.get(jobId, {observe: true});
    const delay = nonNegativeInt(waitMs, 0);
    if (job.state === 'running' && delay > 0) {
      let timer;
      await Promise.race([
        job.done,
        new Promise((resolve) => { timer = setTimeout(resolve, delay); }),
      ]);
      if (timer) clearTimeout(timer);
    }
    if (job.state === 'running' && job.launcherExited) await this.refreshAfterLauncherExit(job);
    return this.snapshot(job);
  }

  async signalOwnedGroup(job, signal) {
    if (job.ownershipReleased || job.pgid == null) return false;
    if (process.platform === 'win32') {
      if (job.process.exitCode != null) return false;
      return job.process.kill(signal);
    }
    const inspected = await this.inspectOwnedGroup(job);
    if (inspected.status === 'gone') return false;
    if (inspected.status !== 'alive') {
      throw new Error(`Cannot safely verify ownership of process group ${job.pgid}; refusing to signal`);
    }
    try {
      process.kill(-job.pgid, signal);
      return true;
    } catch (error) {
      if (error?.code === 'ESRCH') {
        const after = await this.inspectOwnedGroup(job);
        if (after.status === 'gone') return false;
      }
      throw error;
    }
  }

  async waitForOwnedGroupGone(job, timeoutMs) {
    const deadline = Date.now() + Math.max(0, timeoutMs);
    while (true) {
      const inspected = await this.inspectOwnedGroup(job);
      if (inspected.status === 'gone') return true;
      if (Date.now() >= deadline) return false;
      await sleep(Math.min(25, Math.max(1, deadline - Date.now())));
    }
  }

  async stop(jobId, reason = 'stopped') {
    const job = this.get(jobId);
    if (job.state !== 'running') return this.snapshot(job);
    job.stopReason = reason;

    const before = await this.inspectOwnedGroup(job);
    if (before.status === 'gone') {
      this.finalizeStopped(job, reason);
      return this.snapshot(job);
    }
    if (before.status !== 'alive') {
      job.stopReason = null;
      throw new Error(`Cannot safely verify ownership of process group ${job.pgid}; refusing to stop job`);
    }

    await this.signalOwnedGroup(job, 'SIGTERM');
    if (this.stopGraceMs > 0 && await this.waitForOwnedGroupGone(job, this.stopGraceMs)) {
      this.finalizeStopped(job, reason);
      return this.snapshot(job);
    }

    const afterTerm = await this.inspectOwnedGroup(job);
    if (afterTerm.status === 'alive') await this.signalOwnedGroup(job, 'SIGKILL');
    else if (afterTerm.status !== 'gone') {
      job.stopReason = null;
      throw new Error(`Lost safe ownership evidence for process group ${job.pgid} during stop`);
    }

    if (await this.waitForOwnedGroupGone(job, Math.max(100, this.stopGraceMs))) {
      this.finalizeStopped(job, reason);
      return this.snapshot(job);
    }
    job.stopReason = null;
    throw new Error(`Owned process group ${job.pgid} did not terminate`);
  }

  async reapOnce(now = this.now()) {
    this.purgeExpiredCompleted(now);
    for (const job of [...this.jobs.values()]) {
      if (job.state !== 'running' || job.persistent) continue;
      if (job.launcherExited) await this.refreshAfterLauncherExit(job);
      if (job.state !== 'running') continue;
      if (now - job.lastOutputAtMs < this.idleLeaseMs || now - job.lastObservedAtMs < this.idleLeaseMs) continue;

      const inspected = await this.inspectOwnedGroup(job);
      if (inspected.status === 'gone') {
        if (job.launcherExited) this.finalizeNaturalCompletion(job);
        continue;
      }
      if (inspected.status !== 'alive') {
        // Cannot prove safe ownership/idleness: keep the job.
        continue;
      }
      const sample = this.activityReader
        ? await this.activityReader(job.pgid, job.sessionId)
        : inspected.activity;
      if (sample === null || sample === undefined) continue;
      if (job.activitySnapshot === null) {
        job.activitySnapshot = sample;
        job.activitySampleAtMs = now;
        continue;
      }
      if (activityChanged(job.activitySnapshot, sample)) {
        job.activitySnapshot = sample;
        job.activitySampleAtMs = now;
        continue;
      }
      if (job.activitySampleAtMs === null || now - job.activitySampleAtMs < this.busySampleWindowMs) continue;
      await this.stop(job.jobId, 'reaped');
    }
  }
}

export const DEFAULTS = Object.freeze({
  outputLimitBytes: DEFAULT_OUTPUT_LIMIT_BYTES,
  maxJobs: DEFAULT_MAX_JOBS,
  idleLeaseMs: DEFAULT_IDLE_LEASE_MS,
  sweepMs: DEFAULT_SWEEP_MS,
  completedTtlMs: DEFAULT_COMPLETED_TTL_MS,
  stopGraceMs: DEFAULT_STOP_GRACE_MS,
});
