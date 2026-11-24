"""Prompt registry: modules, immutable published versions, hashing, diffs, lineage.

- Published versions are immutable; edits create new versions.
- Protected modules (safety/isolation) cannot be edited by agents.
- Rollback/restore always creates a NEW version.
- Historical prompts are never reconstructed from latest templates: every run
  stores the fully rendered prompt (see provider.RenderedPrompt).
"""
from __future__ import annotations

import difflib
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import PromptModule, PromptVersion


class ProtectedPromptError(RuntimeError):
    pass


class ImmutableVersionError(RuntimeError):
    pass


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_or_create_module(session: Session, key: str, category: str,
                         description: str = "", is_protected: bool = False) -> PromptModule:
    module = session.execute(select(PromptModule).where(PromptModule.key == key)).scalars().first()
    if module is None:
        module = PromptModule(key=key, category=category, description=description,
                              is_protected=is_protected)
        session.add(module)
        session.flush()
    return module


def create_version(session: Session, module: PromptModule, template_text: str,
                   variables: list[str] | None = None, created_by: str = "human",
                   status: str = "published",
                   parent_version_id: str | None = None) -> PromptVersion:
    if module.is_protected and created_by == "agent":
        raise ProtectedPromptError(
            f"Prompt module '{module.key}' is protected; autonomous agents may not edit it.")
    latest = session.execute(
        select(PromptVersion).where(PromptVersion.module_id == module.id)
        .order_by(PromptVersion.version.desc())).scalars().first()
    version_number = (latest.version + 1) if latest else 1
    row = PromptVersion(
        module_id=module.id, version=version_number, status=status,
        template_text=template_text, variables_json=variables or [],
        content_hash=content_hash(template_text),
        parent_version_id=parent_version_id or (latest.id if latest else None),
        created_by=created_by)
    session.add(row)
    session.flush()
    return row


def update_draft(session: Session, version: PromptVersion, template_text: str,
                 actor: str = "human") -> PromptVersion:
    if version.status == "published":
        raise ImmutableVersionError(
            "Published prompt versions are immutable. Create a new version instead.")
    module = session.get(PromptModule, version.module_id)
    if module.is_protected and actor == "agent":
        raise ProtectedPromptError(f"Prompt module '{module.key}' is protected.")
    version.template_text = template_text
    version.content_hash = content_hash(template_text)
    session.flush()
    return version


def restore_as_new_version(session: Session, module: PromptModule,
                           old_version: PromptVersion, actor: str = "human") -> PromptVersion:
    return create_version(session, module, old_version.template_text,
                          variables=old_version.variables_json, created_by=actor,
                          parent_version_id=old_version.id)


def diff_versions(a: PromptVersion, b: PromptVersion) -> str:
    return "\n".join(difflib.unified_diff(
        a.template_text.splitlines(), b.template_text.splitlines(),
        fromfile=f"v{a.version}", tofile=f"v{b.version}", lineterm=""))


def render(template_text: str, variables: dict[str, str]) -> str:
    """Safe render: missing variables stay as literal placeholders."""
    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    return template_text.format_map(_SafeDict(**{k: str(v) for k, v in variables.items()}))


def get_version(session: Session, version_id: str) -> PromptVersion | None:
    return session.get(PromptVersion, version_id)


def latest_published(session: Session, module_key: str) -> PromptVersion | None:
    module = session.execute(select(PromptModule).where(PromptModule.key == module_key)).scalars().first()
    if not module:
        return None
    return session.execute(
        select(PromptVersion).where(PromptVersion.module_id == module.id,
                                    PromptVersion.status == "published")
        .order_by(PromptVersion.version.desc())).scalars().first()
