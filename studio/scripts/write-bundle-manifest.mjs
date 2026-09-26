import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const studioRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const bundleRoot = path.resolve(studioRoot, '..', 'assets', 'workflow-studio');
const packageJson = JSON.parse(readFileSync(path.join(studioRoot, 'package.json'), 'utf8'));

function compareText(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function walk(directory, relative = '') {
  const files = [];
  for (const entry of readdirSync(directory, { withFileTypes: true }).sort((a, b) => compareText(a.name, b.name))) {
    const childRelative = relative ? `${relative}/${entry.name}` : entry.name;
    const absolute = path.join(directory, entry.name);
    if (entry.isSymbolicLink()) throw new Error(`Refusing to package a symbolic link: ${childRelative}`);
    if (entry.isDirectory()) files.push(...walk(absolute, childRelative));
    else if (entry.isFile() && childRelative !== 'bundle-manifest.json') files.push(childRelative);
    else throw new Error(`Refusing to package a non-regular runtime entry: ${childRelative}`);
  }
  return files;
}

const files = {};
for (const relative of walk(bundleRoot).sort(compareText)) {
  files[relative] = createHash('sha256').update(readFileSync(path.join(bundleRoot, ...relative.split('/')))).digest('hex');
}
const manifest = {
  schema_version: 'workflow-studio-bundle-v1',
  release_version: packageJson.version,
  files,
};
writeFileSync(path.join(bundleRoot, 'bundle-manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
