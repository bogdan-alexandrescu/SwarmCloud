"""Author tests/fixtures/redaction-parity.json. Expected outputs are written by hand."""

import json
from pathlib import Path

MASK = "********"
B = "-----" + "BEGIN RSA PRIVATE KEY" + "-----"
E = "-----" + "END RSA PRIVATE KEY" + "-----"
BODY = [f"K{n:04d}" * 12 + "QQQQ" for n in range(8)]
KEY_LEAK = "K0000K0000"

_KV_SECRET = "hunter2-very-secret"
_KV_BARE = "q8Zr7Lm2Xv9T"
_BASH_C = json.dumps(
    {"command": 'bash -c "export DB_PASSWORD=\\"' + _KV_SECRET + '\\" && ./deploy.sh"'}
)
_CURL_D = json.dumps(
    {"command": 'curl -d "{\\"api_key\\": \\"' + _KV_BARE + '\\"}" https://api'}
)

cases = []


def case(name, text, expected, never, count):
    cases.append({"name": name, "text": text, "expected": expected, "never": never, "count": count})


# -- the key/value rule in JSON text (#221, the PR #229 review), moved here --
case("kv-escaped-export", 'export DB_PASSWORD=\\"' + _KV_SECRET + '\\" && ./deploy.sh',
     'export DB_PASSWORD=\\"********\\" && ./deploy.sh', [_KV_SECRET], 1)
case("kv-escaped-in-stream-json",
     '{"type":"assistant","message":{"content":[{"type":"tool_use","input":'
     '{"command":"export DB_PASSWORD=\\"' + _KV_SECRET + '\\""}}]}}',
     '{"type":"assistant","message":{"content":[{"type":"tool_use","input":'
     '{"command":"export DB_PASSWORD=\\"********\\""}}]}}', [_KV_SECRET], 1)
case("kv-escaped-curl-body",
     "curl -d '{\\\"api_key\\\": \\\"" + _KV_BARE + "\\\"}' https://example.com",
     "curl -d '{\\\"api_key\\\": \\\"********\\\"}' https://example.com", [_KV_BARE], 1)
case("kv-list-first-element", '{"password": ["hunter2-list-value"]}',
     '{"password": ["********"]}', ["hunter2-list"], 1)
case("kv-escaped-list-first-element", '{\\"password\\": [\\"hunter2-list-value\\"]}',
     '{\\"password\\": [\\"********\\"]}', ["hunter2-list"], 1)
case("kv-escaped-key-number", '\\"password\\": 12345678', '\\"password\\": ********', ["12345678"], 1)
case("kv-escaped-key-boolean", '\\"secret\\":true', '\\"secret\\":********', ["true"], 1)
case("kv-escaped-key-bareword", '\\"token\\": abcdefgh', '\\"token\\": ********', ["abcdefgh"], 1)
case("kv-two-escapes-deep-bash-c", _BASH_C, _BASH_C.replace(_KV_SECRET, MASK), [_KV_SECRET], 1)
case("kv-two-escapes-deep-curl-d", _CURL_D, _CURL_D.replace(_KV_BARE, MASK), [_KV_BARE], 1)
case("kv-plain-quoted", 'PASSWORD="bare-value-123"', 'PASSWORD="********"', ["bare-value"], 1)
case("kv-plain-backslash-inside", "password=ab\\cd-still-whole", "password=********", ["cd-still-whole"], 1)
case("kv-plain-prefixed-name", "GH_TOKEN=abcdef0123456789abcdef0123456789", "GH_TOKEN=********",
     ["0123456789abc"], 1)

# -- #224: names the keyword does not END --
case("kv-aws-secret-access-key-env", "AWS_SECRET_ACCESS_KEY=sand-dune-lantern-4471",
     "AWS_SECRET_ACCESS_KEY=********", ["dune-lantern"], 1)
case("kv-aws-credentials-file", "aws_secret_access_key = sand-dune-lantern-4471",
     "aws_secret_access_key = ********", ["dune-lantern"], 1)
case("kv-private-key", "private_key: pale-orchid-ledger-5190", "private_key: ********",
     ["orchid-ledger"], 1)
case("kv-password-hash", "password_hash: pbkdf2-synthetic-0042", "password_hash: ********",
     ["pbkdf2-synthetic"], 1)
case("kv-secret-key", "SECRET_KEY=moss-quartz-anvil-2217", "SECRET_KEY=********", ["quartz-anvil"], 1)
case("kv-plural-secrets", "secrets: plain-db-pass-0042", "secrets: ********", ["plain-db-pass"], 1)
case("kv-plural-api-keys", "API_KEYS=first-synthetic-key-01", "API_KEYS=********",
     ["first-synthetic"], 1)
case("kv-plural-tokens", "tokens: amber-kite-3308", "tokens: ********", ["amber-kite"], 1)
case("kv-plural-credentials-json", '{"credentials": "cedar-walrus-9931"}',
     '{"credentials": "********"}', ["cedar-walrus"], 1)
case("kv-plural-passwords-escaped", '{\\"passwords\\": \\"cedar-walrus-9931\\"}',
     '{\\"passwords\\": \\"********\\"}', ["cedar-walrus"], 1)
# Controls: a count under a wider name is a count, as `JsonMasker` decides for a
# JSON key (`_masks_whole`); a name the keyword only begins is not a credential's.
case("control-max-tokens", "max_tokens=4096", "max_tokens=4096", [], 0)
case("control-usage-counts", '"usage": {"input_tokens": 10, "output_tokens": 5}',
     '"usage": {"input_tokens": 10, "output_tokens": 5}', [], 0)
case("control-escaped-usage-count", '{\\"output_tokens\\":57}', '{\\"output_tokens\\":57}', [], 0)
case("control-revoked-times", "credential_revoked_times: 2", "credential_revoked_times: 2", [], 0)
case("control-secret-name", "secret_name: swarm-tenant-eng-anthropic",
     "secret_name: swarm-tenant-eng-anthropic", [], 0)
case("control-singular-number-still-masked", "password: 12345678", "password: ********",
     ["12345678"], 1)

# -- #227: `api-key` and `X-API-KEY` in the shell filter --
case("kv-x-api-key-header", "x-api-key: c2VydmljZS1rZXktc3ludGhldGlj", "x-api-key: ********",
     ["c2VydmljZS1rZXkt"], 1)
case("kv-api-key-hyphen", "api-key=plum-harbor-7710", "api-key=********", ["plum-harbor"], 1)
case("kv-x-api-key-curl", 'curl -H "X-API-KEY: plum-harbor-7710" https://example.com',
     'curl -H "X-API-KEY: ********" https://example.com', ["plum-harbor"], 1)

# -- #227: `Authorization: token <x>` --
case("authorization-token", "Authorization: token 0123456789abcdef0123",
     "Authorization: ******** ********", ["456789abcdef"], 2)
case("authorization-token-curl",
     'curl -H "Authorization: token 0123456789abcdef0123" https://api.github.com/user',
     'curl -H "Authorization: ******** ********" https://api.github.com/user', ["456789abcdef"], 2)
case("authorization-token-escaped", '{\\"Authorization\\": \\"token 0123456789abcdef0123\\"}',
     '{\\"Authorization\\": \\"******** ********\\"}', ["456789abcdef"], 2)
case("authorization-short-bearer", "Authorization: Bearer short-tok",
     "Authorization: ******** ********", ["short-tok"], 2)

# -- #206: a private key printed as text is a BLOCK in both filters --
case("pem-block-over-many-lines",
     "\n".join(["before the key", B, *BODY[:6], E, "after the key"]),
     "\n".join(["before the key", B + MASK, "after the key"]), [KEY_LEAK, "K0005"], 1)
case("pem-cut-short-stops-at-the-first-line-that-is-not-key",
     "\n".join([B, *BODY[:4], "", "Error: the file ended early"]),
     "\n".join([B + MASK, "", "Error: the file ended early"]), [KEY_LEAK, "K0003"], 1)
case("pem-numbered-listing-whole",
     "\n".join(f"{n + 1:6d}\t{line}" for n, line in enumerate([B, *BODY[:4], E, "after"])),
     "\n".join([f"{1:6d}\t{B}{MASK}", f"{7:6d}\tafter"]), [KEY_LEAK, "K0003"], 1)
case("pem-numbered-listing-cut",
     "\n".join(f"{n + 1:6d}\t{line}" for n, line in enumerate([B, *BODY[:4], "ordinary words"])),
     "\n".join([f"{1:6d}\t{B}{MASK}", f"{6:6d}\tordinary words"]), [KEY_LEAK, "K0003"], 1)
case("pem-text-that-starts-inside-a-key",
     "\n".join([*BODY[2:6], E, "after"]),
     "\n".join([MASK + E, "after"]), ["K0002K0002", "K0005"], 1)
case("pem-end-with-material-before-it-on-its-line",
     "tail of key AAAABBBBCCCC" + E + " trailing",
     "tail of key " + MASK + E + " trailing", ["AAAABBBB"], 1)
case("pem-encrypted-headers-cut-short",
     "\n".join([B, "Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,00FF00FF00FF00FF", "",
                *BODY[:3], "not key material here"]),
     "\n".join([B + MASK, "not key material here"]), [KEY_LEAK, "ENCRYPTED", "00FF00FF"], 1)
case("pem-key-on-one-json-line",
     json.dumps({"type": "user", "content": "\n".join([B, *BODY[:4], E, ""])}) + "\nthe next line",
     '{"type": "user", "content": "' + B + MASK + "\nthe next line", [KEY_LEAK], 1)
case("pem-two-keys-with-text-between",
     "\n".join([B, *BODY[:2], E, "middle line", B, *BODY[4:6], E]),
     "\n".join([B + MASK, "middle line", B + MASK]), [KEY_LEAK, "K0004K0004"], 2)

doc = {
    "_comment": [
        "ONE FIXTURE SET FOR BOTH REDACTION FILTERS: swarm_api.redaction.redact and",
        "redact() in scripts/lib/common.sh. Each case is run through both, and both",
        "must give `expected` exactly; `never` must survive neither; `count` is what",
        "the Python filter reports. Read by tests/unit/control_plane/test_log_redaction.py",
        "and by scripts/lib/check-contract-parity.sh (section 8).",
        "The private-key markers are written with \\u002d for their first dash, so no",
        "tracked file holds a PEM header a secret scanner (security.yml) would flag;",
        "every JSON reader decodes it to the real marker. Every value is synthetic.",
    ],
    "cases": cases,
}
text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
text = text.replace("-----BEGIN", "\\u002d----BEGIN").replace("-----END", "\\u002d----END")
assert "-----BEGIN" not in text
Path("tests/fixtures/redaction-parity.json").write_text(text)
print(len(cases))
