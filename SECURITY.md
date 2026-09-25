# Security Policy

## Scope

Del-Fi is designed to run on a local network or air-gapped mesh. Its primary
attack surface is:

- **The message board** — user-submitted content injected into LLM context
- **The gossip directory** — announcements received from untrusted mesh nodes
- **The sensor feed** — externally written `sensor_feed.json` file
- **Peer-cached answers** — Q&A content from peer nodes

Known mitigations are documented in `.claude/spec-memory.md` (board injection
filtering) and `.claude/spec-knowledge.md` (peer trust model).

## Reporting a vulnerability

If you find a security issue — particularly around prompt injection, sandbox
escape, or malicious content reaching LLM context — please report it privately
before opening a public issue.

**Contact:** Open a [GitHub Security Advisory](../../security/advisories/new)
(preferred), or email the maintainer directly if you cannot use GitHub.

Please include:
- A description of the vulnerability and its impact
- Steps to reproduce
- Any suggested mitigations

We aim to acknowledge reports within 72 hours and resolve confirmed issues
within 30 days.

## Threat model notes

Del-Fi is community infrastructure for low-bandwidth mesh networks, not a
production web service. The deployment model assumes:

- **The radio is untrusted.** Any mesh node can send a message. Rate limiting
  and content filtering are the primary defences.
- **The operator's knowledge files are trusted.** Files in `knowledge/` are
  loaded directly without sanitisation. Do not put untrusted content there.
- **The LLM is local.** There is no remote API key to steal. Ollama runs
  entirely on the operator's hardware.
- **Peer nodes are semi-trusted.** Peer-cached answers are labelled with the
  source node. Only hardware node IDs listed in `mesh_knowledge.peers` can
  contribute to the Tier 2 cache; display names never grant trust.
- **Gossip is untrusted metadata.** Announcements (opt-in) only produce
  referrals ("Try NODE (!id) — covers …"), never answers. They are
  sanitised, keyed by sender node ID, and the directory is size-capped.
- **The board is untrusted.** Only posts relevant to a question reach the
  LLM prompt, flattened to one line each, inside markers with a random
  per-prompt nonce so a post cannot close the untrusted block; posts are
  also filtered for common injection phrasings. The board should not be
  the only defence for a high-stakes deployment.
- **The GUI is local-only.** It binds to 127.0.0.1 and rejects cross-site
  requests; reach a headless node's GUI through an SSH tunnel.

## Out of scope

- Vulnerabilities in Ollama, ChromaDB, or Meshtastic firmware
- Attacks requiring physical access to the host machine
- General LLM hallucination (this is a known limitation, not a security bug)
