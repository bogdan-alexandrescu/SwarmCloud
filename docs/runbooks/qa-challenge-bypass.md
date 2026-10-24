# Let SwarmCloud's visual QA past the CAPTCHA on an app you own

CAPTCHA-BYPASS, part of #2058. Design:
[docs/design/visual-qa-any-app.md](../design/visual-qa-any-app.md) §2.5.
For the **owner of the application** a QA target tests, and the workspace
admin who registered the target.

**Why it exists.** A CAPTCHA, or a WAF or bot-management challenge, is a site
owner saying no to automated access. SwarmCloud respects that: its live gate
never solves a challenge, never sends one to a solver service or a model, and
never disguises the browser to slip past one. A sign-in that meets a challenge
ends `qa_sign_in_challenge` (§2.4), and a screen behind one is captured as the
challenge page. On **your own** application you can say yes instead, with the
tools your CAPTCHA vendor and your edge already give you. SwarmCloud then
carries your answer to the page: the gate adds the header (or cookie) you
choose, only on the target's approved origins, and the agent never sees its
value.

**What it is not.** It is no way past a challenge on an application whose
owner did not set one up, and SwarmCloud offers none. If you do not control
the app's CAPTCHA configuration or its edge rules, stop here and ask its owner.

## Choose one route per environment

| environment | what you change in your app | what SwarmCloud needs |
|---|---|---|
| staging, dev, preview | the CAPTCHA widget's **site key and secret key** become the vendor's published always-pass test keys | usually nothing: the test keys pass every browser. Configure a bypass only if a WAF challenge sits in front of the app as well |
| production | a WAF or edge rule that **skips** the challenge for requests carrying a header (or cookie) whose value only you and SwarmCloud know | the bypass on the QA target: its kind, its name, and its value |

Never put test keys in production. Every vendor below says the same: they
turn the CAPTCHA off for everyone.

## Staging: the vendors' always-pass test keys

Each vendor publishes a test site key and a test secret key that always pass,
for exactly this use. Copy them from the vendor's own page. This runbook
reproduces none of them, so that what you configure is what the vendor
publishes today.

* **Google reCAPTCHA.** [reCAPTCHA FAQ](https://developers.google.com/recaptcha/docs/faq),
  "I'd like to run automated tests with reCAPTCHA. What should I do?". For v2,
  the published test keys always pass, and the widget shows a warning that
  it is a test key. For v3, the page says to create a separate key for the
  test environment instead: no score-based key is always-pass, so a v3
  staging app may still need the production route below.
* **hCaptcha.** [hCaptcha developer guide](https://docs.hcaptcha.com/#integration-testing-test-keys),
  "Integration Testing: Test Keys". A test site key and secret pair that
  always passes. Enterprise accounts get further pairs that simulate a
  detected bot, which is useful for testing your own failure path, and is not
  what a QA target should use.
* **Cloudflare Turnstile.** [Turnstile testing](https://developers.cloudflare.com/turnstile/troubleshooting/testing/).
  Dummy site keys, one of which always passes, work on any domain, and a
  dummy secret key accepts only the dummy tokens they produce.

Then check it the way the run will see it: open the staging page in a private
window. The widget should pass with no puzzle, and show the vendor's test-key
notice where it has one.

## Production: a skip rule that matches your header

1. **Make the value.** At least 16 and at most 1024 characters, from what both
   a header and a cookie carry unquoted: letters, digits and
   ``!#$%&'()*+-./:<=>?@[]^_`{|}~``. No space, quote, comma, semicolon,
   backslash or line break. Generate it from randomness, for example
   `openssl rand -hex 32`, and paste it straight into your edge's
   configuration and into the console (step 3). Never paste it into a chat,
   an issue, a pull request, a prompt or this repository.
2. **Add the rule at your edge**, matching the header's exact value and
   skipping only the challenge:
   * **Cloudflare WAF:** a custom rule with the
     [Skip action](https://developers.cloudflare.com/waf/custom-rules/skip/),
     expression `http.request.headers["x-qa-bypass"][0] eq "<your value>"`
     (lower-case header name), skipping the products that challenge
     (Managed Rules, Super Bot Fight Mode, rate limiting). Cloudflare's page
     notes that Bot Fight Mode, the Free plan's, cannot be skipped. On that
     plan, use the staging route or turn Bot Fight Mode off.
   * **Google Cloud Armor:** a rule whose
     [expression](https://docs.cloud.google.com/armor/docs/rules-language-reference)
     is `request.headers['x-qa-bypass'] == '<your value>'` (header names are
     lower case there), with action `allow` and a priority above your
     reCAPTCHA or bot-management rule.
   * **AWS WAF:** a rule with a
     [string match statement](https://docs.aws.amazon.com/waf/latest/developerguide/waf-rule-statement-type-string-match.html)
     on the single header `x-qa-bypass`, "Exactly matches string", action
     `Allow`, placed before the CAPTCHA or Challenge rule.
   * **Your own app code** (a CAPTCHA check in your server): compare the
     header with a constant-time comparison before calling the vendor's verify
     endpoint, and skip the check only when it matches.

   Prefer a header. A cookie works too (SwarmCloud plants it host-only,
   `Secure` and `HttpOnly`), but some edges read cookies less directly.
3. **Configure the QA target.** A workspace admin, in the console:
   Repositories › QA targets › the target's **Edit**. Set **Challenge
   bypass** to header (or cookie) and the name (for example `X-QA-Bypass`).
   Never use a name your app already uses for something else: `Authorization`,
   `Cookie`, `Host`, `Proxy-*`, `Sec-*` and the request-framing headers are
   refused. Type the value into **Value (write-only)** and save. The card
   then says `Challenge bypass · header X-QA-Bypass · value set`, with when
   and by whom, and never shows the value again.
   That form sends the value without a one-time link, which the API logs as
   `challenge bypass set without a link` and lets through while the
   `qa_bypass_link_required` switch is off (its default). Leave it off until
   the console's bypass form opens from a link
   (`POST /v1/qa-targets/{id}/credential-links {"field": "challenge_bypass"}`,
   lane SSC-UI): on, a value sent without one is refused 403.
4. **Check it.** Run the target (Run, with "report only, file no issues").
   A screen that showed the challenge before now shows the page. Your edge's
   logs show the rule matching requests from the run.

## What SwarmCloud does with it

* The value is stored in your workspace's QA secret
  (`swarm-tenant-<workspace>-qa`), beside the target's test credential, and
  read only by swarm-api. No page, route or log shows it.
* For each run, the live gate gets it with the target and adds the header
  **only** to requests for the target's approved origins: its own address,
  and the login's extra origin if a platform admin approved that. No
  third-party request (analytics, a CDN on another host, a payment frame)
  ever carries it.
* The agent never receives it. The gate scrubs it from everything bound for
  the agent, as it scrubs the test password. A page that prints its request
  headers into the page itself would show the value in a screenshot, so a
  target whose app echoes request headers should not be given a bypass.

## Rotate or remove it

* **Rotate:** change the value at your edge to accept both the old and the
  new value, type the new one into the target's **Value (write-only)** and
  save, then remove the old one from your edge.
* **Remove:** set **Challenge bypass** to None and save. SwarmCloud deletes
  the value from the secret. Then delete the rule at your edge.
* **The address changes:** changing the target's host (or its login's extra
  origin) deletes the value too, because a value made for one host is never
  sent to another. Set it again once the new host is approved.
