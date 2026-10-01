// `npm run build` in the acceptance fixture: writes a file, then prints the
// marker the suite asserts. dist/ is in .gitignore, so it never reaches the
// task's harvested patch.
const fs = require("node:fs");

fs.mkdirSync("dist", { recursive: true });
fs.writeFileSync("dist/out.txt", "built\n");
console.log("ACCEPTANCE-NPM-BUILD-OK");
