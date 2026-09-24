export function sameStrategy(left, right) {
  const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
  if (!object(left) || !object(right) || left.id !== right.id || !object(left.parameters) || !object(right.parameters)) return false;
  const keys = Object.keys(left.parameters);
  return keys.length === Object.keys(right.parameters).length
    && keys.every((key) => Object.hasOwn(right.parameters, key) && left.parameters[key] === right.parameters[key]);
}
