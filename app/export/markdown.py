"""Markdown rendering. Generic over artefact shape.

Walks whatever the schema produced rather than knowing about specific formats,
so a new registry entry using renderer "markdown" works with no change here.
"""

from __future__ import annotations


def to_markdown(artefact: dict) -> str:
    lines: list[str] = []
    claims = artefact.get("claims") or []

    for key, value in artefact.items():
        if key == "claims":
            continue
        lines.extend(_render(key, value))

    if claims:
        # Citations are the point of the whole pipeline; surface them.
        lines.append("\n---\n")
        lines.append("### Sources\n")
        for claim in claims:
            lines.append(f'- "{claim.get("text", "")}" — `{claim.get("chunk_id", "")}`')

    return "\n".join(lines).strip() + "\n"


def _render(key: str, value, depth: int = 0) -> list[str]:
    label = key.replace("_", " ").title()

    if isinstance(value, str):
        if not value.strip():
            return []
        if depth == 0 and key in ("title", "headline", "hook"):
            return [f"# {value}\n" if key == "title" else f"**{value}**\n"]
        return [f"### {label}\n", f"{value}\n"] if depth == 0 else [f"{value}\n"]

    if isinstance(value, (int, float)):
        return [f"**{label}:** {value}\n"]

    if isinstance(value, list):
        if not value:
            return []
        out = [f"### {label}\n"] if depth == 0 else []
        for item in value:
            if isinstance(item, str):
                out.append(f"- {item}")
            elif isinstance(item, dict):
                out.append("")
                for k, v in item.items():
                    if isinstance(v, list):
                        out.append(f"**{k.replace('_', ' ').title()}:**")
                        out.extend(f"  - {i}" for i in v)
                    else:
                        out.append(f"**{k.replace('_', ' ').title()}:** {v}")
        out.append("")
        return out

    if isinstance(value, dict):
        out = [f"### {label}\n"]
        for k, v in value.items():
            out.extend(_render(k, v, depth + 1))
        return out

    return []
