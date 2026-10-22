import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const runnerSource = new URL('./baseline_vitest_runner.mjs', import.meta.url);

// Baseline failures are measurements, but failed hooks/incomplete runs are not.
export default class BaselineReporter {
  errors = [];
  activeHooks = new Map();

  onInit(vitest) {
    for (const project of vitest.projects) {
      if (project.config.runner) {
        throw new Error('Baseline cleanup instrumentation cannot replace a configured custom runner');
      }
      if (project.config.mode !== 'test' || project.config.browser?.enabled) {
        throw new Error('Baseline cleanup instrumentation requires the Node test runner');
      }
      // Vite's proxy middleware cannot serve an external /@fs runner reliably.
      // A disposable node_modules copy is loaded as a dependency instead.
      const cache = join(project.config.root, 'node_modules', '.cache');
      mkdirSync(cache, { recursive: true });
      const directory = mkdtempSync(join(cache, 'baseline-vitest-'));
      vitest.onClose(() => rmSync(directory, { recursive: true, force: true }));
      const runner = join(directory, 'runner.mjs');
      copyFileSync(runnerSource, runner);
      project.config.runner = runner;
      project.serializedConfig.runner = runner;
    }
  }

  onHookStart({ entity, name }) {
    const key = `${entity.id}:${name}`;
    if (this.activeHooks.has(key)) this.errors.push(`hook:${name}`);
    this.activeHooks.set(key, name);
  }

  onHookEnd({ entity, name }) {
    // Vitest 4's ReportedHookContext has only entity/name, not a hook result.
    // The runner records successful hooks as "pass" and leaves failures "run".
    if (entity.task?.result?.hooks?.[name] !== 'pass') {
      this.errors.push(`hook:${name}`);
    }
    this.activeHooks.delete(`${entity.id}:${name}`);
  }

  onTestRunEnd(modules, unhandledErrors, reason) {
    const errors = [...this.errors];
    if (!['passed', 'failed'].includes(reason)) errors.push(`reason:${reason}`);
    if (unhandledErrors.length) errors.push('unhandled');
    for (const name of this.activeHooks.values()) errors.push(`hook:${name}`);
    let tests = 0;
    let failures = 0;
    const visit = (entity) => {
      // Inspect every level: suite/module errors are not descendant test failures.
      if (entity.type === 'test') {
        tests += 1;
        const { state } = entity.result();
        if (state === 'failed') failures += 1;
        else if (!['passed', 'skipped'].includes(state)) errors.push('incomplete');
      } else {
        if (entity.errors().length) errors.push(`${entity.type}:errors`);
        if (!['passed', 'failed', 'skipped'].includes(entity.state())) {
          errors.push('incomplete');
        }
        for (const child of entity.children) visit(child);
      }
      // Failed hooks do not emit onHookEnd; also check the final runner state.
      for (const [name, state] of Object.entries(entity.task?.result?.hooks ?? {})) {
        if (state !== 'pass') errors.push(`hook:${name}`);
      }
    };
    for (const module of modules) {
      const cleanup = module.task?.meta?.baselineCleanup;
      if (cleanup?.version !== 1 || cleanup.complete !== true || !Array.isArray(cleanup.errors)) {
        errors.push('cleanup:missing-instrumentation');
      } else {
        errors.push(...cleanup.errors.map(name => `cleanup:${name}`));
      }
      visit(module);
    }
    if (!tests) errors.push('no-tests');
    const path = process.env.BASELINE_STATUS_FILE;
    if (path) {
      writeFileSync(path, `${JSON.stringify({
        complete: errors.length === 0, tests, failures, errors,
      })}\n`);
    }
  }
}
