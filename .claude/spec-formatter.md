# Del-Fi — Formatter Specification

<!-- Parent: .claude/claude.md §2.1 (230-byte limit) -->
<!-- Source of truth: del_fi/core/formatter.py -->
<!-- Related: spec-router.md §4 (!more buffer), §8 (first-contact footer); spec-mesh.md (send path) -->

---

## 1. Purpose

`del_fi/core/formatter.py` turns text into LoRa-sized messages. It is a set of
pure functions: no state, no I/O, no Ollama. The router calls one of two
entry points for every reply:

| Function | Used for | Markdown stripped |
|----------|----------|-------------------|
| `format_response(text, max_bytes, provenance=None)` | LLM answers (Tier 1, Tier 2) | yes |
| `paginate(text, max_bytes)` | command output (`!board`, `!topics`, `!data`, `!peers`, …) | no — board posts are user text |

Both return `(first_message, all_chunks, is_truncated)`.

---

## 2. Byte Limit

`max_response_bytes` (default 230, validated 50–256; see spec-config §2.5).
Every size is a UTF-8 byte count (`byte_len(text)`), never a character
count. Byte slices are decoded with `errors="ignore"`, so a cut never splits a
multi-byte character (`°`, `—`, emoji).

---

## 3. Cleaning (`format_response` only)

`clean_text(text)` = `strip_markdown` then `collapse_whitespace`.

`strip_markdown`, in order:

| Input | Result |
|-------|--------|
| fenced code block (```` ``` … ``` ````) | removed entirely, content included |
| `**bold**`, `*italic*`, `` `code` `` | inner text |
| `# Heading` (levels 1–6) | `Heading` |
| `[text](url)` | `text` (an image `![alt](url)` becomes `!alt`) |
| `> quote` | `quote` |
| `---` / `***` / `___` rule lines | removed |
| `- item`, `* item`, `+ item`, `1. item` | `item` |

Not handled: `__bold__`, `_italic_`, `~~strike~~` pass through unchanged.

`collapse_whitespace`: runs of two or more newlines become one space, single
newlines are kept, runs of spaces/tabs become one space, and the result is
trimmed.

---

## 4. Truncation — `truncate_at_sentence(text, max_bytes)`

If the text fits, it is returned unchanged. Otherwise it is cut to
`max_bytes` (UTF-8-safe) and then back to the last boundary inside the cut,
trying in order:

1. sentence end — `.` `!` `?` followed by whitespace or the end
2. clause end — `.` `!` `?` `;` `:` `—` `…` followed by whitespace or the end, or `... `
3. the last space
4. hard cut (the UTF-8-safe slice itself)

The result is stripped.

---

## 5. Chunking

`chunk_text(text, max_bytes)` repeatedly takes `truncate_at_sentence` of the
remaining text. If that comes back empty it forces a UTF-8-safe hard cut; if
even that is empty (a single character wider than `max_bytes`), it stops
rather than loop forever.

`chunk_lines(text, max_bytes)` packs whole lines into chunks, so a board post
or sensor reading is not split mid-line when avoidable. A line longer than
`max_bytes` is split with `chunk_text`.

---

## 6. `format_response`

1. `clean_text`; if nothing is left, the reply is `(no response)`.
2. With `provenance` (a Tier 2 peer answer), the text is prefixed with
   `[via NAME] ` and **truncated** to one message (when at least 21 bytes
   remain for the answer).
3. If it fits in `max_bytes`: one message, `is_truncated = False`.
4. Otherwise `chunk_text` with a budget of `max_bytes − 7`, leaving room for
   the `" [!more]"` tag (`MORE_TAG`) on any chunk.
5. One chunk: returned as a single message. Several: the first message is
   `chunks[0] + " [!more]"` and `is_truncated = True`.

## 7. `paginate`

Same shape, without markdown stripping: fits → one message; otherwise
`chunk_lines` with budget `max_bytes − 7`. If that yields a single chunk,
the whole text is truncated to one message instead.

---

## 8. What the Router Does With the Result

See spec-router §4 and §8. In short:

- `is_truncated`: the chunks become the sender's `!more` buffer (expires
  after 600 s). The first `auto_send_chunks` (default 3) are sent at once,
  0.5 s apart; only the last of them keeps `" [!more]"`, and only if more
  chunks remain. `!more` sends exactly one further chunk; `!more N` re-sends
  chunk N.
- Not truncated, sender's first contact: a footer (`"\n---\n"` +
  `welcome_footer`, or `Del-Fi oracle · N pages · !help !topics`) is
  appended only if the message still fits `max_bytes`.

There is no per-message sign-off (such as `// NODE-NAME`) and no
`+N !more` counter; an earlier draft of this spec described both.

**Send-time safety net:** `MeshtasticAdapter.send_dm` re-splits any message
over `max_response_bytes` with `chunk_text` and sends the parts 3 s apart.
It should never trigger, because the router formats every reply first.

---

## 9. Testing

`tests/test_formatter.py` covers markdown stripping (bold, italic, inline
code, headings, links, code blocks, blockquotes, lists), whitespace
collapsing, `byte_len` (ASCII, multi-byte, empty), truncation (fits,
sentence ends, word-boundary fallback, no content lost), chunking (single,
split, all content preserved) and `format_response` (fits, strips markdown,
`[!more]` tag, provenance tag, provenance truncation, empty and
whitespace-only input). `tests/test_router.py` covers `paginate` through long
command output (`test_long_command_output_is_chunked_with_more`) and the
`!more` buffer behaviour.

---

<!-- End of spec-formatter.md -->
