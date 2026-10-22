import { AsyncLocalStorage } from 'node:async_hooks';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// Resolve from the measured checkout, never from this script's checkout.
const require = createRequire(resolve(process.cwd(), 'package.json'));
const version = require('vitest/package.json').version;
if (version !== '4.1.11') {
  throw new Error(`Baseline cleanup instrumentation requires Vitest 4.1.11, got ${version}`);
}
const entry = resolve(require.resolve('vitest/package.json'), '../dist/index.js');
const { TestRunner } = await import(pathToFileURL(entry).href);
const context = new AsyncLocalStorage();

export default class BaselineRunner extends TestRunner {
  tests = new Map();
  guarded = new WeakSet();
  attempts = new WeakMap();

  onBeforeTryTask(test, options) {
    let attempt = this.attempts.get(test);
    if (!attempt) {
      attempt = { cleanup: false, states: [], normalized: false };
      this.attempts.set(test, attempt);
      let state = test.result.state;
      Object.defineProperty(test.result, 'state', {
        configurable: true,
        enumerable: true,
        get: () => state,
        set: value => {
          if (attempt.cleanup) attempt.states.push(value);
          state = value;
        },
      });
    }
    attempt.cleanup = false;
    return super.onBeforeTryTask(test, options);
  }

  onTaskFinished(test) {
    // Vitest calls this after the body (including assertion-count checks), before
    // teardown. Observe writes, not final state: retries/test.fails can erase them.
    this.attempts.get(test).cleanup = true;
  }

  onAfterRunTask(test) {
    // In 4.1.11 the last state write before this hook is test.fails normalization.
    const attempt = this.attempts.get(test);
    if (attempt) attempt.normalized = !!test.fails;
    return super.onAfterRunTask(test);
  }

  constructor(config) {
    super(config);
    const trace = this.trace;
    this.trace = (name, attributes, callback) => {
      const run = typeof attributes === 'function' ? attributes : callback;
      const wrapped = () => {
        const test = context.getStore();
        if (name === 'test.onFinished' || name === 'test.onFailed') {
          const key = name === 'test.onFinished' ? 'onFinished' : 'onFailed';
          test[key] = test[key].map(fn => this.guard(fn, test.file, key));
        }
        return run();
      };
      const invoke = name === 'run.test'
        ? () => context.run(this.tests.get(attributes['vitest.test.id']), wrapped)
        : wrapped;
      return typeof attributes === 'function'
        ? trace(name, invoke)
        : trace(name, attributes, invoke);
    };
  }

  guard(fn, file, name) {
    if (this.guarded.has(fn)) return fn;
    const runner = this;
    const wrapped = async function (...args) {
      try {
        const result = await fn.apply(this, args);
        return typeof result === 'function' ? runner.guard(result, file, `${name}:cleanup`) : result;
      } catch (error) {
        file.meta.baselineCleanup.errors.push(name);
        throw error;
      }
    };
    // Around-hook timeout/fixture metadata lives on the original function.
    for (const key of Reflect.ownKeys(fn)) {
      if (!['name', 'length', 'prototype', 'caller', 'arguments'].includes(key)) {
        Object.defineProperty(wrapped, key, Object.getOwnPropertyDescriptor(fn, key));
      }
    }
    this.guarded.add(wrapped);
    return wrapped;
  }

  async onCollected(files) {
    await super.onCollected?.(files);
    const visit = (task, file) => {
      if (task.type === 'test') this.tests.set(task.id, task);
      else {
        const hooks = TestRunner.getSuiteHooks(task);
        for (const name of Object.keys(hooks)) {
          hooks[name] = hooks[name].map(fn => this.guard(fn, file, name));
        }
        for (const child of task.tasks) visit(child, file);
      }
    };
    for (const file of files) {
      file.meta.baselineCleanup = { version: 1, complete: false, errors: [] };
      visit(file, file);
    }
  }

  async onAfterRunFiles(files) {
    await super.onAfterRunFiles(files);
    for (const test of this.tests.values()) {
      const attempt = this.attempts.get(test);
      if (!attempt || !files.includes(test.file)) continue;
      const states = attempt.normalized ? attempt.states.slice(0, -1) : attempt.states;
      if (states.includes('fail')) test.file.meta.baselineCleanup.errors.push('teardown');
    }
    for (const file of files) file.meta.baselineCleanup.complete = true;
    // The normal final task update precedes this hook; explicitly send metadata.
    await this.onTaskUpdate(files.map(file => [file.id, file.result, file.meta]), []);
  }
}
