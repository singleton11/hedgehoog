export default function GoalProgressBar({ achieved, label }) {
  // The fill stops at the target, while the accompanying value stays uncapped.
  const fill = Math.max(0, Math.min(100, Number.parseFloat(achieved)));
  return <progress className="goal-progress-bar" max="100" value={fill}
    aria-label={label} aria-valuetext={`${achieved} of goal achieved`} />;
}
