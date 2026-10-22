// Keep artifact lookup separate so exact-commit selection can be tested offline.
module.exports = async function findBaseline({ github, owner, repo, sha, branch, suite }) {
  if (branch !== 'main') {
    throw new Error(`Artifact baselines are only published for main, not ${branch}`);
  }
  for await (const page of github.paginate.iterator(github.rest.actions.listWorkflowRuns, {
    owner, repo, workflow_id: 'ci.yml', event: 'push', branch,
    head_sha: sha, status: 'completed', per_page: 100,
  })) {
    for (const run of page.data) {
      if (run.head_sha !== sha || run.head_branch !== branch || run.event !== 'push' || run.status !== 'completed') continue;
      const artifacts = await github.paginate(github.rest.actions.listWorkflowRunArtifacts, {
        owner, repo, run_id: run.id, per_page: 100,
      });
      if (artifacts.some(a => a.name === `baseline-${suite}` && !a.expired)) {
        return String(run.id);
      }
    }
  }
  throw new Error(`No retained baseline-${suite} artifact for main commit ${sha}. Run push CI for that commit before retrying.`);
};
