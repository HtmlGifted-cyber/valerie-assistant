# Specialists

Specialists are expert profiles Valerie can hand a job to. Ask for "the SEO specialist" to improve a product title, or "the email strategist" to draft a welcome email, and you get that expert's answer back in the chat.

A specialist is a Markdown file with a short header and a long body. The body is the expert's instructions. Valerie reads the header to find the right one, then sends the body plus your task to your own AI brain.

## What it is

- A specialist does one job well: it answers your task directly, with the expert's method, and nothing else.
- It is a single model call with no tools. A specialist can write, plan and advise, but it cannot send mail, change files or take any other action.
- It runs on the brain you already use: OmniRoute (free models) or Ollama on your computer. No extra account is needed.

## Get a pack

The easiest free pack is **agency-agents**, a collection of department folders (marketing, sales, engineering, security and more):

```bash
git clone https://github.com/msitarzewski/agency-agents
```

Then point Valerie at the department folders you want in `~/.config/valerie-assistant/config.json`:

```json
"specialists": {
  "folders": [
    "/path/agency-agents/marketing",
    "/path/agency-agents/sales"
  ]
}
```

Each folder listed is scanned for `*.md` files directly inside it, so list each department folder rather than the repository root.

You can also skip the config and copy the `.md` files into `~/.claude/agents`. If `folders` is empty, Valerie uses `~/.claude/agents` and `./.claude/agents` when they exist.

## Any Markdown agent file works

Any file in this format is a specialist, including the agent files Claude Code uses:

```markdown
---
name: SEO Specialist
description: Expert search engine optimization strategist for technical SEO and content
color: "#D97706"
emoji: 🔎
vibe: Rankings come from useful pages that search engines can understand
---

# SEO Specialist

You are an SEO specialist. ...
```

Only `name` and `description` are used for finding a specialist. If `name` is missing, the file name is used. Quoted or unquoted values both work.

## Using specialists

Ask Valerie in plain words:

- "Ask the SEO specialist to improve this product title: Cozy Cats Coloring Book".
- "Have the email strategist draft a welcome email for new newsletter readers."
- "Find a specialist for security review."

Behind the scenes Valerie uses two tools:

- `find_specialist` searches the installed profiles by topic and lists the best matches with their names.
- `use_specialist` sends a task to one named specialist (or to the best match for a topic) and returns its answer, labelled with the specialist's name.

From a terminal:

```bash
python va_specialists.py list                 # departments and counts
python va_specialists.py list email marketing # best matches for a topic
python va_specialists.py run "SEO Specialist" "Give 3 better titles for a children's coloring book about cozy cats"
```

## Settings

```json
"specialists": {
  "folders": ["/path/agency-agents/marketing"],
  "max_prompt_chars": 9000
}
```

- `folders`: folders of Markdown agent files. Empty means `~/.claude/agents` and `./.claude/agents`.
- `max_prompt_chars`: the longest specialist body sent to the model. Longer bodies are cut short.

Each call waits up to 5 minutes. Free OmniRoute routes can be slow; if OmniRoute fails, the call falls back to Ollama.

## Credit and license

The agency-agents pack is by [msitarzewski](https://github.com/msitarzewski/agency-agents) and is released under the MIT license. It is **not bundled** with Valerie. You download it yourself, and its own license file applies to it. Valerie only reads the files; it does not copy, change or redistribute them.
