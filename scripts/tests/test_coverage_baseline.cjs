const { test } = require('node:test');
const assert = require('node:assert/strict');
const findBaseline = require('../find_coverage_baseline.cjs');

function fixture(pages, artifacts) {
  const calls = [];
  const github = {
    rest: { actions: { listWorkflowRuns: 'runs', listWorkflowRunArtifacts: 'artifacts' } },
    paginate: Object.assign(async (method, args) => {
      calls.push([method, args]);
      return artifacts[args.run_id] || [];
    }, {
      iterator: async function* (method, args) {
        calls.push([method, args]);
        for (const data of pages) yield { data };
      },
    }),
  };
  return { args: { github, owner: 'owner', repo: 'repo', sha: 'base-sha', branch: 'main', suite: 'backend' }, calls };
}
const run = (id, extra = {}) => ({ id, head_sha: 'base-sha', head_branch: 'main', event: 'push', status: 'completed', ...extra });
const artifact = (extra = {}) => ({ name: 'baseline-backend', expired: false, ...extra });

test('uses exact commit, workflow, repository and completed push runs, including failed runs', async () => {
  const { args, calls } = fixture([[run(12, { conclusion: 'failure' })]], { 12: [artifact()] });
  assert.equal(await findBaseline(args), '12');
  assert.deepEqual(calls[0], ['runs', {
    owner: 'owner', repo: 'repo', workflow_id: 'ci.yml', event: 'push', branch: 'main',
    head_sha: 'base-sha', status: 'completed', per_page: 100,
  }]);
  assert.deepEqual(calls[1], ['artifacts', { owner: 'owner', repo: 'repo', run_id: 12, per_page: 100 }]);
});

test('skips wrong commits, branches, events and incomplete runs', async () => {
  const { args, calls } = fixture([[
    run(1, { head_sha: 'latest-main' }), run(2, { head_branch: 'other' }),
    run(3, { event: 'pull_request' }), run(4, { status: 'in_progress' }), run(5),
  ]], { 5: [artifact()] });
  assert.equal(await findBaseline(args), '5');
  assert.equal(calls.length, 2);
});

test('paginates and skips missing, expired and wrong-suite artifacts', async () => {
  const { args } = fixture([[run(1), run(2)], [run(3), run(4)]], {
    1: [], 2: [artifact({ expired: true })],
    3: [artifact({ name: 'baseline-frontend' })], 4: [artifact()],
  });
  assert.equal(await findBaseline(args), '4');
});

test('fails closed when no retained matching artifact exists', async () => {
  const { args } = fixture([[run(1)]], {});
  await assert.rejects(findBaseline(args), /No retained baseline-backend artifact for main commit base-sha/);
});

test('rejects unsupported target branches before querying API', async () => {
  const { args, calls } = fixture([], {});
  await assert.rejects(findBaseline({ ...args, branch: 'release' }), /only published for main/);
  assert.equal(calls.length, 0);
});

test('does not hide API failures', async () => {
  const { args } = fixture([[run(1)]], {});
  args.github.paginate = Object.assign(async () => { throw new Error('Forbidden'); }, { iterator: args.github.paginate.iterator });
  await assert.rejects(findBaseline(args), /Forbidden/);
});
