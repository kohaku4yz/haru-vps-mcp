import {z} from 'zod';
import {jsonResult} from '../utils/response.js';
import {strictSchemaWithAliases} from '../utils/schema.js';
import {ManagedJobRegistry} from './managed-jobs.js';

const DEFAULT_WAIT_MS = 5000;
const MAX_WAIT_MS = 25000;
const MAX_HARD_TIMEOUT_MS = 7 * 24 * 60 * 60 * 1000;
const jobs = new ManagedJobRegistry();

const executeDescription = `Run a command in bash as a managed job.

The command is tracked before Haru waits for it. wait_ms controls only how long this call waits synchronously (default 5s, max 25s); wait expiry never kills the command. If the job finishes in time, the familiar stdout/stderr/exitCode result is returned. Otherwise a stable jobId and bounded output-so-far are returned for later inspection with get_job_status.

Set persistent=true for an intentionally quiet long-lived foreground service so the ordinary idle reaper will not stop it. hard_timeout_ms is optional and is the only timeout here that means process lifetime. background=true remains supported as an immediate managed-job return.`;

const statusDescription = `Get a managed shell job status and bounded output. Reading status refreshes the observation lease for a running job. Completed/stopped job records expire automatically after their retention TTL.`;

const stopDescription = `Explicitly stop a managed shell job. The backend sends SIGTERM to the owned Unix process group, waits a short grace period, then sends SIGKILL if needed. Descendants that deliberately create a new session/process group can escape this best-effort boundary.`;

function quickResult(result) {
  return {
    stdout: result.stdout,
    stderr: result.stderr,
    exitCode: result.exitCode,
    stdoutTruncated: result.stdoutTruncated,
    stderrTruncated: result.stderrTruncated,
    stdoutBytes: result.stdoutBytes,
    stderrBytes: result.stderrBytes,
  };
}

export function registerBash(server) {
  server.registerTool(
    'execute',
    {
      title: 'Execute',
      description: executeDescription,
      inputSchema: strictSchemaWithAliases(
        {
          command: z.string().describe('The bash command to run'),
          wait_ms: z.number().int().min(0).max(MAX_WAIT_MS).optional().describe(`How long to wait synchronously before returning a job ID (default ${DEFAULT_WAIT_MS}ms, max ${MAX_WAIT_MS}ms). This does not kill the job.`),
          persistent: z.boolean().optional().describe('Exempt this intentionally long-lived quiet job from ordinary idle reaping'),
          hard_timeout_ms: z.number().int().min(1).max(MAX_HARD_TIMEOUT_MS).optional().describe('Optional explicit maximum process lifetime. Unlike wait_ms, expiry stops the job.'),
          background: z.boolean().optional().describe('Return the managed job ID immediately (legacy background mode)'),
        },
        {
          timeout: 'wait_ms',
          timeout_ms: 'wait_ms',
        },
      ),
    },
    async (args) => {
      const waitMs = args.background ? 0 : (args.wait_ms ?? DEFAULT_WAIT_MS);
      const job = jobs.start(args.command, {
        persistent: args.persistent ?? false,
        hardTimeoutMs: args.hard_timeout_ms ?? null,
      });
      const result = await jobs.wait(job.jobId, waitMs);

      if (!result.running && !args.background) {
        jobs.releaseCompleted(job.jobId);
        return jsonResult(quickResult(result));
      }
      return jsonResult(result);
    },
  );

  server.registerTool(
    'get_job_status',
    {
      title: 'Get Job Status',
      description: statusDescription,
      inputSchema: strictSchemaWithAliases(
        {jobId: z.string().uuid().describe('The managed job ID returned by execute')},
        {job_id: 'jobId'},
      ),
    },
    async (args) => jsonResult(await jobs.status(args.jobId)),
  );

  server.registerTool(
    'stop_job',
    {
      title: 'Stop Job',
      description: stopDescription,
      inputSchema: strictSchemaWithAliases(
        {jobId: z.string().uuid().describe('The managed job ID returned by execute')},
        {job_id: 'jobId'},
      ),
    },
    async (args) => jsonResult(await jobs.stop(args.jobId)),
  );
}

export {jobs as managedJobs};
