"""
self_edit.py - lets Valerie maintain her own skills and knowledge base.

Scope is deliberately narrow: she can only write inside her own
skills/ and knowledge/ folders (never the Google Drive mirror or any
other document). Creating a new file happens straight away; changing or
deleting an existing one needs the owner's Yes (via the confirm callback),
and the previous version is always kept in a .versions/ folder first.
"""

import datetime
import difflib
import os
import re
import shutil

import config as cfg_mod
import knowledge
import skills as skills_mod


class EditError(Exception):
    pass


def _slug(name):
    s = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower().replace(" ", "-")).strip("-")
    if not s:
        raise EditError("A name is required.")
    return s[:60]


def _keep_version(path):
    if not os.path.exists(path):
        return
    vdir = os.path.join(os.path.dirname(path), ".versions")
    os.makedirs(vdir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base, ext = os.path.splitext(os.path.basename(path))
    shutil.copy2(path, os.path.join(vdir, f"{base}.{stamp}{ext}"))


def _diff(old, new):
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "current", "new", lineterm="", n=1))
    text = "\n".join(lines[2:])
    return text[:1500] + ("\n..." if len(text) > 1500 else "")


def _approve(confirm, title, text):
    if confirm is None:
        raise EditError("Changing or deleting needs the owner's approval in the Valerie window.")
    if not confirm(title, text):
        raise EditError("Declined - nothing was changed.")


def _write(path, content, confirm, what):
    """Create, or (after approval) replace, one file. Returns a status line."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
        if old.strip() == content.strip():
            return f"No change - {what} already says that."
        _approve(confirm, f"Update {what}?", _diff(old, content) or content[:1500])
        _keep_version(path)
        verb = "Updated"
    else:
        verb = "Created"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")
    return f"{verb} {what}."


def _delete(path, confirm, what):
    if not os.path.exists(path):
        raise EditError(f"{what} doesn't exist.")
    _approve(confirm, f"Delete {what}?", f"{what} will be removed (a copy is kept in .versions).")
    _keep_version(path)
    os.remove(path)
    return f"Deleted {what} (backup kept in .versions)."


# ------------------------------------------------------------------ skills

def skill_manage(action, name=None, description=None, prompt=None, confirm=None):
    sdir = cfg_mod.SKILLS_DIR
    if action == "list":
        sk = skills_mod.load_skills()
        return "\n".join(f"/{s['name']} - {s['description']}" for s in sk.values()) or "No skills yet."
    slug = _slug(name)
    path = os.path.join(sdir, f"{slug}.md")
    if action == "read":
        if not os.path.exists(path):
            raise EditError(f"No skill named '{slug}'.")
        with open(path, encoding="utf-8") as f:
            return f.read()
    if action == "save":
        if not prompt:
            raise EditError("A skill needs a prompt (use {input} where the user's details go).")
        if os.path.exists(path) and not description:
            description = skills_mod.load_skills().get(slug, {}).get("description", "")
        body = prompt.strip()
        if "{input}" not in body:
            body += "\n\nDetails: {input}"
        content = f"name: {slug}\ndescription: {(description or slug).strip()}\n---\n{body}\n"
        return _write(path, content, confirm, f"skill /{slug}") + f" Use it as /{slug} <details>."
    if action == "delete":
        return _delete(path, confirm, f"skill /{slug}")
    raise EditError("action must be list, read, save or delete.")


# --------------------------------------------------------------- knowledge

def _kb_path(name):
    slug = _slug(re.sub(r"\.md$", "", name or "", flags=re.I))
    return os.path.join(cfg_mod.KNOWLEDGE_DIR, f"{slug}.md"), slug


def kb_manage(action, name=None, content=None, confirm=None):
    kdir = cfg_mod.KNOWLEDGE_DIR
    if action == "list":
        files = sorted(f for f in os.listdir(kdir) if f.endswith(".md")) if os.path.isdir(kdir) else []
        return ("Valerie's own knowledge files (editable):\n" + "\n".join(f"  {f[:-3]}" for f in files)
                + "\n(Google Drive documents are also searchable but read-only.)") if files else "No knowledge files yet."
    path, slug = _kb_path(name)
    what = f"knowledge file '{slug}'"
    if action == "read":
        if not os.path.exists(path):
            raise EditError(f"No {what}.")
        with open(path, encoding="utf-8") as f:
            return f.read()[:6000]
    if action in ("save", "append"):
        if not content or not content.strip():
            raise EditError("Content is required.")
        if action == "append":
            if not os.path.exists(path):
                new = f"# {slug.replace('-', ' ').title()}\n\n{content.strip()}\n"
            else:
                with open(path, encoding="utf-8") as f:
                    new = f.read().rstrip() + "\n\n" + content.strip() + "\n"
        else:
            new = content if content.lstrip().startswith("#") else f"# {slug.replace('-', ' ').title()}\n\n{content.strip()}\n"
        status = _write(path, new, confirm, what)
    elif action == "delete":
        status = _delete(path, confirm, what)
    else:
        raise EditError("action must be list, read, save, append or delete.")
    knowledge.build_index()
    pinned = [os.path.realpath(os.path.expanduser(p)) for p in cfg_mod.load_config().get("assistant", {}).get("pinned_files", [])]
    if os.path.realpath(path) in pinned:
        status += " (This is a core-rules file - it applies from my next message.)"
    return status
