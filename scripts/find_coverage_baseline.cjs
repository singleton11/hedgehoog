// Prefer the exact base, then the nearest first-parent ancestor, never run time.
module.exports = async function findBaseline({ github, owner, repo, sha, branch, suite }) {
  if (branch !== 'main') {
    throw new Error(`Artifact baselines are only published for main, not ${branch}`);
  }
  const workflow = { owner, repo, workflow_id: 'ci.yml', per_page: 100 };
  // Filtered search can omit an existing run; widen discovery, not eligibility.
  const queries = [
    { ...workflow, event: 'push', branch, head_sha: sha, status: 'completed' },
    workflow,
  ];
  const candidates = new Map();
  const checked = new Set();
  async function hasArtifact(run) {
    if (checked.has(run.id)) return false;
    checked.add(run.id);
    const artifacts = await github.paginate(github.rest.actions.listWorkflowRunArtifacts, {
      owner, repo, run_id: run.id, per_page: 100,
    });
    return artifacts.some(a => a.name === `baseline-${suite}` && !a.expired);
  }
  for (const query of queries) {
    for await (const page of github.paginate.iterator(github.rest.actions.listWorkflowRuns, query)) {
      for (const run of page.data) {
        if (run.head_branch !== branch || run.event !== 'push' || run.status !== 'completed') continue;
        if (run.head_sha === sha) {
          if (await hasArtifact(run)) return { runId: String(run.id), sha, distance: 0 };
        } else {
          if (!candidates.has(run.head_sha)) candidates.set(run.head_sha, new Map());
          candidates.get(run.head_sha).set(run.id, run);
        }
      }
    }
  }
  let current = sha;
  let distance = 0;
  const visited = new Set([sha]);
  while (candidates.size) {
    const { data } = await github.rest.repos.getCommit({ owner, repo, ref: current });
    const parent = data.parents[0]?.sha;
    if (!parent || visited.has(parent)) break;
    visited.add(parent);
    current = parent;
    distance += 1;
    for (const run of candidates.get(current)?.values() || []) {
      if (await hasArtifact(run)) return { runId: String(run.id), sha: current, distance };
    }
    candidates.delete(current);
  }
  throw new Error(`No retained baseline-${suite} artifact for main commit ${sha} or its first-parent ancestors.`);
};
