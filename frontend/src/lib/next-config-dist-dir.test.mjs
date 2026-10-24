import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

function loadConfigWithDistDir(value) {
  const configPath = require.resolve('../../next.config.js');
  const previous = process.env.NEXT_DIST_DIR;
  delete require.cache[configPath];

  if (value === undefined) {
    delete process.env.NEXT_DIST_DIR;
  } else {
    process.env.NEXT_DIST_DIR = value;
  }

  try {
    return require(configPath);
  } finally {
    delete require.cache[configPath];
    if (previous === undefined) {
      delete process.env.NEXT_DIST_DIR;
    } else {
      process.env.NEXT_DIST_DIR = previous;
    }
  }
}

test('next config leaves default distDir unchanged when NEXT_DIST_DIR is absent', () => {
  const config = loadConfigWithDistDir(undefined);

  assert.equal(config.distDir, undefined);
});

test('next config uses NEXT_DIST_DIR for isolated verification builds', () => {
  const config = loadConfigWithDistDir('.next-verify');

  assert.equal(config.distDir, '.next-verify');
});
