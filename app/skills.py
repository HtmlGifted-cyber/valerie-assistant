"""
skills.py - reusable prompt "skills" for Valerie.

A skill is a Markdown file in ~/.local/share/valerie-assistant/skills/
with a two-line header and a prompt body:

    name: kdp-metadata
    description: Write KDP title, subtitle, description, keywords, categories
    ---
    Write Amazon KDP metadata for this book: {input}
    ...

Use it in chat as "/kdp-metadata A coloring book of cozy cats". The
body's {input} is replaced with whatever follows the command. Add a
new skill by dropping another .md file in the folder - no code changes.
"""

import os

import config as cfg_mod


def load_skills():
    skills = {}
    if not os.path.isdir(cfg_mod.SKILLS_DIR):
        return skills
    for fname in sorted(os.listdir(cfg_mod.SKILLS_DIR)):
        if not fname.endswith(".md"):
            continue
        try:
            with open(os.path.join(cfg_mod.SKILLS_DIR, fname), encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        head, sep, body = raw.partition("\n---\n")
        if not sep:
            head, body = "", raw
        meta = {}
        for line in head.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        name = meta.get("name") or fname[:-3]
        skills[name] = {"name": name, "description": meta.get("description", ""), "prompt": body.strip()}
    return skills


def render(skill, user_input):
    prompt = skill["prompt"]
    if "{input}" in prompt:
        return prompt.replace("{input}", user_input.strip() or "(no details given)")
    return f"{prompt}\n\n{user_input}".strip()
