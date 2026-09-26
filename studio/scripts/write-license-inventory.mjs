import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const studioRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const lock = JSON.parse(readFileSync(path.join(studioRoot, 'package-lock.json'), 'utf8'));
const lockPackages = lock.packages;
const rootPackage = lockPackages?.[''];
if (!rootPackage || !rootPackage.dependencies || !lockPackages) {
  throw new Error('package-lock.json must contain a root package and a packages map.');
}

function packageLocation(name, fromLocation) {
  let cursor = fromLocation;
  while (true) {
    const candidate = cursor ? `${cursor}/node_modules/${name}` : `node_modules/${name}`;
    if (Object.hasOwn(lockPackages, candidate)) return candidate;
    if (!cursor) break;
    cursor = path.posix.dirname(cursor);
    if (cursor === '.') cursor = '';
  }
  throw new Error(`The lockfile does not resolve production dependency ${name}.`);
}

function licenseExpression(value) {
  if (typeof value === 'string' && value.trim()) return value.trim();
  if (Array.isArray(value)) {
    const expressions = value.map(licenseExpression).filter(Boolean);
    return expressions.length ? expressions.join(' OR ') : '';
  }
  if (value && typeof value === 'object' && typeof value.type === 'string') return value.type.trim();
  return '';
}

function repositoryUrl(value) {
  if (typeof value === 'string') return value.trim();
  if (value && typeof value === 'object' && typeof value.url === 'string') return value.url.trim();
  return '';
}

function compareText(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

const visitedLocations = new Set();
const entriesByIdentity = new Map();
const pending = Object.keys(rootPackage.dependencies).map((name) => [name, '']);
while (pending.length) {
  const [name, fromLocation] = pending.pop();
  const location = packageLocation(name, fromLocation);
  if (visitedLocations.has(location)) continue;
  visitedLocations.add(location);

  const locked = lockPackages[location];
  if (!locked || typeof locked.version !== 'string') throw new Error(`Missing locked version for ${name}.`);
  const installedPath = path.join(studioRoot, ...location.split('/'));
  let installed;
  try {
    installed = JSON.parse(readFileSync(path.join(installedPath, 'package.json'), 'utf8'));
  } catch (error) {
    throw new Error(`Installed production dependency metadata is missing for ${name}@${locked.version}: ${String(error)}`);
  }
  if (installed.name !== name || installed.version !== locked.version) {
    throw new Error(`Installed package metadata does not match package-lock.json for ${name}@${locked.version}.`);
  }
  const license = licenseExpression(installed.license) || licenseExpression(locked.license);
  if (!license) throw new Error(`Production dependency ${name}@${locked.version} has no declared license.`);

  const entry = {
    name,
    version: locked.version,
    license,
    homepage: typeof installed.homepage === 'string' ? installed.homepage.trim() : typeof locked.homepage === 'string' ? locked.homepage.trim() : '',
    repository: repositoryUrl(installed.repository) || repositoryUrl(locked.repository),
  };
  const identity = `${name}@${locked.version}`;
  const previous = entriesByIdentity.get(identity);
  if (previous && previous.license !== entry.license) {
    throw new Error(`Conflicting declared licenses for ${identity}.`);
  }
  entriesByIdentity.set(identity, entry);

  const childLocation = location;
  for (const childName of Object.keys(locked.dependencies ?? {})) {
    pending.push([childName, childLocation]);
  }
  for (const childName of Object.keys(locked.optionalDependencies ?? {})) {
    pending.push([childName, childLocation]);
  }
}

const packages = [...entriesByIdentity.values()].sort((left, right) =>
  compareText(left.name, right.name) || compareText(left.version, right.version),
);
const inventory = {
  schema_version: 'workflow-studio-third-party-licenses-v1',
  packages,
};
writeFileSync(
  path.join(studioRoot, '..', 'assets', 'workflow-studio', 'THIRD_PARTY_LICENSES.json'),
  `${JSON.stringify(inventory, null, 2)}\n`,
  'utf8',
);
