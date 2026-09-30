// `npm test` in the acceptance fixture. The marker is what the suite asserts
// in the runner's stdout: an exit status of 0 alone would not show this file
// is what ran.
const assert = require("node:assert");

assert.strictEqual([1, 2, 3].reduce((a, b) => a + b, 0), 6);
console.log("ACCEPTANCE-NPM-TEST-OK");
