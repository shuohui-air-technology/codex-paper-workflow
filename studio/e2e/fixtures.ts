import { spawn } from 'node:child_process';
import { connect } from 'node:net';
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test as base, expect, type BrowserContext, type Page } from '@playwright/test';

interface StudioSession {
  context: BrowserContext;
  page: Page;
  url: string;
  origin: string;
  externalRequests: string[];
}

interface StartupRecord { status: 'ready'; url: string; port: number }

const REPOSITORY_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const STARTUP_TIMEOUT_MS = 15_000;

async function readStartupRecord(child: ReturnType<typeof spawn>): Promise<StartupRecord> {
  let stderr = '';
  child.stderr?.on('data', (chunk: Buffer) => { stderr = (stderr + chunk.toString()).slice(-12_000); });
  return new Promise((resolveRecord, rejectRecord) => {
    let buffer = '';
    let settled = false;
    const finish = (error?: Error, record?: StartupRecord) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      child.stdout?.off('data', onData);
      child.off('error', onError);
      child.off('exit', onExit);
      if (error) rejectRecord(error);
      else resolveRecord(record!);
    };
    const onData = (chunk: Buffer) => {
      buffer += chunk.toString();
      const newline = buffer.indexOf('\n');
      if (newline < 0) return;
      try {
        const parsed = JSON.parse(buffer.slice(0, newline)) as StartupRecord;
        if (parsed.status !== 'ready' || typeof parsed.url !== 'string' || !Number.isInteger(parsed.port)) {
          throw new Error('The local Studio launcher returned an invalid readiness record.');
        }
        finish(undefined, parsed);
      } catch (error) {
        finish(new Error(`The local Studio launcher did not start cleanly: ${String(error)} ${stderr}`));
      }
    };
    const onError = (error: Error) => finish(error);
    const onExit = (code: number | null) => finish(new Error(`Studio exited before becoming ready (${code}): ${stderr}`));
    const timeout = setTimeout(() => finish(new Error(`Studio did not become ready within ${STARTUP_TIMEOUT_MS} ms: ${stderr}`)), STARTUP_TIMEOUT_MS);
    child.stdout?.on('data', onData);
    child.once('error', onError);
    child.once('exit', onExit);
  });
}

function waitForExit(child: ReturnType<typeof spawn>, timeoutMs: number): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
  return new Promise((resolveExit) => {
    const timer = setTimeout(() => {
      child.off('exit', onExit);
      resolveExit(false);
    }, timeoutMs);
    const onExit = () => {
      clearTimeout(timer);
      resolveExit(true);
    };
    child.once('exit', onExit);
  });
}

function probeLoopbackPort(port: number): Promise<boolean> {
  return new Promise((resolveConnection) => {
    const socket = connect(port, '127.0.0.1');
    const finish = (connected: boolean) => {
      socket.destroy();
      resolveConnection(connected);
    };
    socket.setTimeout(1000, () => finish(true));
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
  });
}

export const test = base.extend<{ studio: StudioSession }>({
  studio: async ({ browser }, use) => {
    const tempRoot = mkdtempSync(join(realpathSync(tmpdir()), 'paper-workflow-studio-e2e-'));
    if (!tempRoot.startsWith(realpathSync(tmpdir()) + '/') || !tempRoot.includes('paper-workflow-studio-e2e-')) {
      throw new Error('Refusing to use an unexpected temporary test directory.');
    }
    const projectRoot = join(tempRoot, 'paper-project');
    const skillsRoot = join(tempRoot, 'installed-skills');
    const codexHome = join(tempRoot, 'isolated-codex-home');
    const testSkillId = 'paper-test-skill';
    mkdirSync(projectRoot);
    mkdirSync(codexHome);
    mkdirSync(join(skillsRoot, testSkillId), { recursive: true });
    writeFileSync(join(skillsRoot, testSkillId, 'SKILL.md'), [
      '---',
      `name: ${testSkillId}`,
      'description: A harmless test-only workflow task.',
      '---',
      'This fixture is used only for browser tests.',
      '',
    ].join('\n'), 'utf8');

    const launcher = join(REPOSITORY_ROOT, 'scripts', 'workflow_studio.py');
    const child = spawn(process.env.PYTHON ?? 'python3', [
      launcher,
      '--project', projectRoot,
      '--skills-root', skillsRoot,
      '--no-browser',
      '--idle-timeout', '120',
    ], {
      cwd: REPOSITORY_ROOT,
      env: { ...process.env, CODEX_HOME: codexHome },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let startup: StartupRecord | null = null;
    let context: BrowserContext | null = null;
    try {
      startup = await readStartupRecord(child);
      const parsedUrl = new URL(startup.url);
      const origin = parsedUrl.origin;
      const externalRequests: string[] = [];
      context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await context.route('**/*', async (route) => {
        const requestUrl = new URL(route.request().url());
        if (requestUrl.protocol !== 'http:' || requestUrl.hostname !== '127.0.0.1') {
          externalRequests.push(route.request().url());
          await route.abort('blockedbyclient');
          return;
        }
        await route.continue();
      });
      const page = await context.newPage();
      await page.goto(startup.url);
      await expect(page.getByRole('heading', { name: '工作流编排器' })).toBeVisible();
      await use({ context, page, url: startup.url, origin, externalRequests });
      await page.waitForTimeout(150);
      expect(externalRequests, 'Studio should not attempt requests outside 127.0.0.1').toEqual([]);
    } finally {
      if (context && startup) {
        try {
          const sessionToken = new URL(startup.url).hash.slice(1).split('&')
            .map((part) => part.split('='))
            .find(([key]) => key === 'session')?.[1];
          if (sessionToken) {
            const authorization = `Bearer ${decodeURIComponent(sessionToken)}`;
            const bootstrapResponse = await context.request.get(`http://127.0.0.1:${startup.port}/api/bootstrap`, {
              headers: { Authorization: authorization },
              timeout: 3000,
            });
            const bootstrapPayload = await bootstrapResponse.json() as { data?: { csrf_token?: string } };
            const csrf = bootstrapPayload.data?.csrf_token;
            if (csrf) {
              await context.request.post(`http://127.0.0.1:${startup.port}/api/shutdown`, {
                headers: {
                  Authorization: authorization,
                  'X-Workflow-CSRF': csrf,
                  Origin: `http://127.0.0.1:${startup.port}`,
                },
                data: {},
                timeout: 3000,
              });
            }
          }
        } catch {
          // The bounded process-exit fallback below still owns this exact test child.
        }
      }
      await context?.close();
      if (!(await waitForExit(child, 5000))) {
        child.kill('SIGINT');
        if (!(await waitForExit(child, 3000))) child.kill('SIGTERM');
      }
      const exited = await waitForExit(child, 2000);
      if (startup && exited && await probeLoopbackPort(startup.port)) {
        throw new Error('Studio test server exited but its loopback port is still accepting connections.');
      }
      if (tempRoot.includes('paper-workflow-studio-e2e-')) rmSync(tempRoot, { recursive: true, force: true });
    }
  },
});

export { expect };
