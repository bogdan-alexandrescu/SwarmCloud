// WHO MAY FRAME THE CONSOLE: the Saga apps hub shows it in a frame from its
// two hosts, and no other site may. Read as text, because CI does not run the
// container. An add_header inside a location drops every server-level one, so
// the location that serves index.html (the page every UI route falls back to)
// must carry the policy itself; before this test it carried none and the
// console went out with no frame rule at all.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const FRAME_POLICY = `add_header Content-Security-Policy "frame-ancestors 'self' https://hub.saga.xyz https://hub.galatia.saga.xyz" always;`;

const nginx = readFileSync(
  join(__dirname, "..", "..", "..", "..", "images", "swarm-ui", "nginx.conf"),
  "utf8",
)
  .split("\n")
  .filter((l) => !l.trimStart().startsWith("#"))
  .join("\n");

function block(opening: string): string {
  const start = nginx.indexOf(opening);
  expect(start, `${opening} is in nginx.conf`).toBeGreaterThanOrEqual(0);
  return nginx.slice(start, nginx.indexOf("}", start));
}

describe("the swarm-ui frame headers", () => {
  it("lets only this origin and the Saga apps hub frame the page", () => {
    const page = block("location = /index.html {");
    expect(page).toContain(FRAME_POLICY);
    expect(page).toContain(
      'add_header X-Content-Type-Options "nosniff" always;',
    );
    expect(page).toContain('add_header Referrer-Policy "same-origin" always;');
  });

  it("sends the same rule from the server level, and no X-Frame-Options", () => {
    const serverLevel = nginx.slice(
      nginx.lastIndexOf("}", nginx.lastIndexOf("}") - 1),
    );
    expect(serverLevel).toContain(FRAME_POLICY);
    expect(nginx).not.toMatch(/X-Frame-Options/i);
    expect(nginx.match(/frame-ancestors[^"]*/g)).toEqual([
      "frame-ancestors 'self' https://hub.saga.xyz https://hub.galatia.saga.xyz",
      "frame-ancestors 'self' https://hub.saga.xyz https://hub.galatia.saga.xyz",
    ]);
  });
});
