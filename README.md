# Synthesia Interactive Avatar — Quickstarts

Runnable recipes for [Synthesia Interactive Avatars](https://www.synthesia.io/features/avatars/interactive-avatars): photoreal, lip-synced avatars you can talk to in your browser.

Each recipe is self-contained, with its own README, dependencies, and `.env.example`, so you can copy a directory out and build on it directly.

| Recipe | What it shows |
| --- | --- |
| [`minimal/`](minimal/) | The smallest useful app: a Python LiveKit voice agent (STT → LLM → TTS) with the avatar integration in three lines. Start here. |
| [`rag/`](rag/) | A LiveKit agent grounded in a knowledge base (public Wikipedia out of the box, AWS Bedrock for your own corpus). It speaks only from retrieved source material. |
| [`tools/`](tools/) | LLM uses tools to fill a booking form in the browser as you speak, and your edits flow back to the agent: tool calling plus UI state sync over LiveKit data channels. |

## Prerequisites

Every recipe needs a **Synthesia API key** for [Interactive Avatars](https://www.synthesia.io/features/avatars/interactive-avatars). Each recipe's README lists everything else it needs: providers, keys, and how to run it.

Every recipe also needs **Python 3.10–3.14**. On macOS, `/usr/bin/python3` is still 3.9 — create the venv with Homebrew (or pyenv) Python, then confirm `python --version` inside the venv before installing.

## Troubleshooting

Recipe READMEs cover plugin-specific errors. These first-run failures show up in every recipe.

### Corporate TLS inspection (`CERTIFICATE_VERIFY_FAILED`)

On an enterprise-managed laptop the frontend often reaches LiveKit while the Python agent does not. The browser shows **Connected — waiting for the avatar to join** and stays there. The agent log retries until `max_retry: 16`, then `RuntimeError: failed to connect to livekit`.

Typical log lines (same cause):

```
[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate
[SSL: CERTIFICATE_VERIFY_FAILED] Basic Constraints of CA cert not marked critical
Cannot connect to host <project>.livekit.cloud:443
```

**The browser connects, Python does not.** Same network, same host, same port. That strongly suggests a runtime trust-store or proxy-configuration problem rather than a general outage or blocked domain. A common cause is a customer's proxy (Zscaler, Netskope, Forcepoint, Palo Alto, and similar) re-signing HTTPS with a corporate root CA. macOS and the browser trust that root; Python normally validates against its bundled `certifi` list, which does not contain it.

Confirm the issuer is the proxy, not a public CA:

```bash
openssl s_client -connect <project>.livekit.cloud:443 -showcerts </dev/null 2>/dev/null | grep -i 'issuer'
```

Turning the proxy agent "off" often leaves the tunnel or PAC file in place until a reboot.

These recipes install [`truststore`](https://pypi.org/project/truststore/) and call `truststore.inject_into_ssl()` at the top of `agent.py`, so Python uses the OS verifier (macOS Keychain / Windows cert store) instead of certifi. That picks up the corporate root automatically.

If you still see `CERTIFICATE_VERIFY_FAILED`, confirm the corporate root is installed and trusted in the OS certificate store. On macOS, `truststore` uses Security.framework and Keychain; it does **not** use `SSL_CERT_FILE`.

If OS trust-store access is unavailable, you can instead use a PEM bundle with Python's standard OpenSSL verifier:

1. Comment out the `truststore` import and `truststore.inject_into_ssl()` call in `agent.py`.
2. Merge the corporate CA and the public roots into one bundle. On macOS:

   ```bash
   security find-certificate -a -p /Library/Keychains/System.keychain > /tmp/corp.pem
   security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain >> /tmp/corp.pem
   security find-certificate -a -p ~/Library/Keychains/login.keychain-db >> /tmp/corp.pem
   cat .venv/lib/python3.*/site-packages/certifi/cacert.pem >> /tmp/corp.pem
   export SSL_CERT_FILE=/tmp/corp.pem
   ```

3. Verify the corporate CA is present:

   ```bash
   openssl crl2pkcs7 -nocrl -certfile /tmp/corp.pem \
     | openssl pkcs7 -print_certs -noout \
     | grep -Ei 'zscaler|netskope|forcepoint|palo alto'
   ```

Do **not** point `SSL_CERT_FILE` at certifi's own `cacert.pem` without adding the corporate CA — that is the bundle already failing. OpenSSL also requires a well-formed CA and complete chain; use the OS trust-store path when a corporate certificate fails strict validation with `Basic Constraints of CA cert not marked critical`.

A phone hotspot is not a fix, but it is the fastest way to prove the stack works (it bypasses corporate DNS, PAC, and any residual tunnel).

**Firewall allowlisting** is separate from certificate trust. LiveKit documents these outbound rules:

| Host | Protocol | Purpose |
| --- | --- | --- |
| `*.livekit.cloud` | TCP 443 | Signaling (WebSocket) |
| `*.turn.livekit.cloud` | TCP 443 | TURN over TLS |
| `*.host.livekit.cloud` | UDP 3478 | TURN over UDP |
| All hosts *(recommended)* | UDP 50000–60000 | WebRTC media |
| All hosts *(recommended)* | TCP 7881 | WebRTC media fallback |

With default-deny exact-match firewalls, the TURN hostnames are not covered by `*.livekit.cloud` alone. For the TCP 443 endpoints, TLS inspection must be bypassed or its corporate root must be trusted by the Python runtime (as above).

### Other first-run traps

| Symptom | Likely cause / fix |
| --- | --- |
| Confusing dependency errors, or the venv cannot install `livekit-agents` | Venv was created with macOS system Python 3.9. Recreate it with 3.10–3.14. |
| Frontend stuck on **Connected — waiting for the avatar to join**, no SSL errors | `python agent.py console` runs a mock room — the avatar never joins. Use `python agent.py dev`. |
| `SyntaxError: invalid syntax` when you run `python server.py` | The command was typed inside a Python REPL. `exit()` first. |
| LiveKit prints that `python agent.py dev` is deprecated | The worker still starts. `lk agent dev` is the [LiveKit CLI](https://docs.livekit.io/reference/developer-tools/livekit-cli/) equivalent; it is optional for these recipes. |
