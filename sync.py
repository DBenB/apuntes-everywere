#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union, Set

# Embedded metadata marker
SYNC_MARKER_PREFIX = "<!-- obsidian-sync:"
SYNC_MARKER_RE = re.compile(r"<!--\s*obsidian-sync:\s*(\{.*?\})\s*-->")

FM_START = "---"
FM_END = "---"

# Image link regex patterns
MD_IMG_RE = re.compile(r'!\[(.*?)\]\((.*?)\)')
WIKI_IMG_RE = re.compile(r'!\[\[(.*?)\]\]')
SUPPORTED_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# Configure basic logging
logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(s: str) -> str:
    s = s.strip().lower()
    s = s.replace("&", " and ")
    s = s.replace("’", "'").replace("'", "")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "post"


def escape_yaml_double_quotes(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def yaml_list(items: List[str]) -> str:
    if not items:
        return "[]"
    quoted = [f'"{escape_yaml_double_quotes(x)}"' for x in items]
    return "[" + ", ".join(quoted) + "]"


def guess_date_from_mtime(path: Path) -> str:
    dt = datetime.fromtimestamp(path.stat().st_mtime)
    return dt.strftime("%Y-%m-%d")


def read_existing_sync_meta(qmd_text: str) -> Optional[dict]:
    m = SYNC_MARKER_RE.search(qmd_text)
    if not m:
        return None
    raw = m.group(1)
    meta = {}
    for k, v in re.findall(r'"([^"]+)"\s*:\s*"([^"]*)"', raw):
        meta[k] = v
    return meta or None


def is_hidden_or_obsidian_internal(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def iter_md_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.md"):
        if is_hidden_or_obsidian_internal(p):
            continue
        yield p


def build_attachment_index(root: Path) -> Dict[str, Path]:
    """Indexes all images in the vault mapping filename -> absolute path."""
    index: Dict[str, Path] = {}
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_IMG_EXTS and not is_hidden_or_obsidian_internal(p):
            index[p.name] = p
    return index


def split_frontmatter(text: str) -> Tuple[Optional[str], str]:
    if not text.startswith(FM_START + "\n"):
        return None, text

    end_idx = text.find("\n" + FM_END + "\n", len(FM_START) + 1)
    if end_idx == -1:
        return None, text

    fm = text[len(FM_START) + 1 : end_idx]
    body = text[end_idx + len("\n" + FM_END + "\n") :]
    return fm, body


def parse_scalar(value: str) -> Union[str, bool]:
    v = value.strip()
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    return v


def parse_frontmatter_minimal(fm_text: str) -> Dict[str, object]:
    data: Dict[str, object] = {}
    lines = fm_text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()
        i += 1
        if not line.strip():
            continue

        m = re.match(r"^([A-Za-z0-9_ -]+):\s*(.*)$", line)
        if not m:
            continue

        key = m.group(1).strip()
        rest = m.group(2).strip()

        if rest != "":
            data[key] = parse_scalar(rest)
            continue

        items: List[str] = []
        while i < len(lines):
            nxt = lines[i].rstrip()
            if re.match(r"^\s+-\s+.+$", nxt):
                item = re.sub(r"^\s+-\s+", "", nxt).strip()
                items.append(item)
                i += 1
                continue
            break
        data[key] = items

    return data


@dataclass
class Note:
    source_path: Path
    title: str
    slug: str
    date: str
    draft: bool
    categories: List[str]
    digital_garden: bool
    body_md: str
    source_hash: str


def build_note(source_path: Path, obsidian_root: Path) -> Optional[Note]:
    try:
        raw = source_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        logger.error(f"Failed to read {source_path.name}. Ensure UTF-8 encoding.")
        return None

    fm_text, body = split_frontmatter(raw)
    fm: Dict[str, object] = parse_frontmatter_minimal(fm_text) if fm_text else {}

    title = str(fm.get("title") or source_path.stem)
    slug = slugify(title)

    date_val = fm.get("date")
    post_date = date_val.strip() if isinstance(date_val, str) and date_val.strip() else guess_date_from_mtime(source_path)

    draft_val = fm.get("draft")
    draft = bool(draft_val) if isinstance(draft_val, bool) else True

    dg_val = fm.get("digital_garden")
    digital_garden = bool(dg_val) if isinstance(dg_val, bool) else False

    tags = fm.get("Additional Tags")
    categories = [str(x) for x in tags] if isinstance(tags, list) else []

    rel_path = source_path.relative_to(obsidian_root).as_posix()
    content_for_hash = f"{rel_path}\n---\n{raw}"
    src_hash = sha256_text(content_for_hash)

    body_md = body.lstrip("\n")

    return Note(
        source_path=source_path,
        title=title,
        slug=slug,
        date=post_date,
        draft=draft,
        categories=categories,
        digital_garden=digital_garden,
        body_md=body_md,
        source_hash=src_hash,
    )


def process_and_copy_images(body_md: str, out_dir: Path, attachment_index: Dict[str, Path], dry_run: bool) -> str:
    """Parses markdown for images, copies them to the Quarto dir, and rewrites the markdown links."""
    
    def handle_image_match(img_name: str, alt_text: str = "") -> str:
        clean_name = img_name.split('|')[0].strip()
        filename = Path(clean_name).name
        
        if filename in attachment_index:
            src_img = attachment_index[filename]
            dst_img = out_dir / filename
            
            if not dst_img.exists() or dst_img.stat().st_mtime < src_img.stat().st_mtime:
                logger.info(f"  -> Copying image: {filename}")
                if not dry_run:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_img, dst_img)
            
            return f"![{alt_text}]({filename})"
            
        return None

    def md_repl(match):
        alt_text = match.group(1)
        img_path_str = match.group(2)
        new_link = handle_image_match(img_path_str, alt_text)
        return new_link if new_link else match.group(0)

    def wiki_repl(match):
        img_path_str = match.group(1)
        new_link = handle_image_match(img_path_str)
        return new_link if new_link else match.group(0)

    body_md = MD_IMG_RE.sub(md_repl, body_md)
    body_md = WIKI_IMG_RE.sub(wiki_repl, body_md)
    
    return body_md


def render_qmd(note: Note, author: Optional[str], obsidian_root: Path, processed_body: str) -> str:
    rel_path = note.source_path.relative_to(obsidian_root).as_posix()
    sync_meta = (
        f'{SYNC_MARKER_PREFIX}{{"source":"{rel_path}",'
        f'"hash":"{note.source_hash}"}} -->'
    )

    yaml_lines: List[str] = [
        "---",
        f'title: "{escape_yaml_double_quotes(note.title)}"',
    ]
    if author:
        yaml_lines.append(f'author: "{escape_yaml_double_quotes(author)}"')

    yaml_lines += [
        f'date: "{note.date}"',
        f"categories: {yaml_list(note.categories)}",
        f"draft: {'true' if note.draft else 'false'}",
        "---",
        "",
    ]

    return "\n".join(yaml_lines) + processed_body.rstrip() + "\n\n" + sync_meta + "\n"


def should_update(dest_path: Path, new_hash: str) -> bool:
    if not dest_path.exists():
        return True
    try:
        existing = dest_path.read_text(encoding="utf-8")
        meta = read_existing_sync_meta(existing)
        if not meta:
            return True
        return meta.get("hash") != new_hash
    except Exception as e:
        logger.warning(f"Could not read existing file {dest_path.name} to check hash: {e}")
        return True


def generate_category_indices(posts_root: Path, synced_paths: Set[Path], dry_run: bool):
    """
    Genera un index.qmd en cada subcarpeta para que Quarto renderice 
    automáticamente un listado ordenado de los posts de esa materia.
    """
    directories = set(path.parent for path in synced_paths)
    
    for dir_path in directories:
        if dir_path == posts_root:
            continue
            
        index_file = dir_path / "index.qmd"
        title = dir_path.name.replace("-", " ").title()
        
        index_content = f"""---
title: "{title}"
listing:
  contents: "*.qmd"
  sort: "date desc"
  type: default
  categories: true
---
"""
        if not index_file.exists() or index_file.read_text(encoding="utf-8") != index_content:
            logger.info(f"Generando índice de categoría: {index_file.name} en {dir_path.name}/")
            if not dry_run:
                index_file.write_text(index_content, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="One-way sync: Obsidian markdown -> Quarto posts (con Image Sync e Índices)"
    )
    ap.add_argument("--obsidian-root", required=True, help="Path to your Obsidian vault")
    ap.add_argument("--quarto-root", required=True, help="Path to your Quarto project root")
    ap.add_argument("--posts-dir", default="Digital Garden", help="Posts directory under Quarto root")
    ap.add_argument("--author", default=None, help="Author name to include in YAML")
    ap.add_argument("--clean", action="store_true", help="Delete orphaned .qmd files")
    ap.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    args = ap.parse_args()

    obsidian_root = Path(args.obsidian_root).expanduser().resolve()
    quarto_root = Path(args.quarto_root).expanduser().resolve()
    posts_root = (quarto_root / args.posts_dir).resolve()

    if not obsidian_root.exists():
        raise SystemExit(f"Obsidian root not found: {obsidian_root}")
    if not quarto_root.exists():
        raise SystemExit(f"Quarto root not found: {quarto_root}")

    posts_root.mkdir(parents=True, exist_ok=True)

    logger.info("Indexing Obsidian attachments...")
    attachment_index = build_attachment_index(obsidian_root)
    logger.info(f"Found {len(attachment_index)} images in vault.")

    created = updated = skipped = ignored = deleted = 0
    synced_qmd_paths: Set[Path] = set()

    for md_path in iter_md_files(obsidian_root):
        note = build_note(md_path, obsidian_root)
        if not note:
            continue

        if not note.digital_garden:
            ignored += 1
            continue

        rel_dir = note.source_path.parent.relative_to(obsidian_root)
        out_dir = posts_root / rel_dir
        out_file = out_dir / f"{note.slug}.qmd"
        
        synced_qmd_paths.add(out_file)

        if should_update(out_file, note.source_hash):
            action = "CREATE" if not out_file.exists() else "UPDATE"
            logger.info(f"{action}: {out_file.name}  <=  {md_path.name}")
            
            processed_body = process_and_copy_images(note.body_md, out_dir, attachment_index, args.dry_run)
            qmd_text = render_qmd(note, args.author, obsidian_root, processed_body)

            if not args.dry_run:
                out_dir.mkdir(parents=True, exist_ok=True)
                out_file.write_text(qmd_text, encoding="utf-8")
                
            if action == "CREATE":
                created += 1
            else:
                updated += 1
        else:
            process_and_copy_images(note.body_md, out_dir, attachment_index, args.dry_run)
            skipped += 1

    generate_category_indices(posts_root, synced_qmd_paths, args.dry_run)

    if args.clean:
        for qmd_file in posts_root.rglob("*.qmd"):
            if qmd_file.name == "index.qmd":
                continue
            if qmd_file not in synced_qmd_paths:
                try:
                    content = qmd_file.read_text(encoding="utf-8")
                    if SYNC_MARKER_RE.search(content):
                        logger.info(f"DELETE (Orphan): {qmd_file.name}")
                        if not args.dry_run:
                            qmd_file.unlink()
                        deleted += 1
                except Exception as e:
                    logger.warning(f"Could not read {qmd_file.name} during cleanup: {e}")

    logger.info(
        f"Done. Created: {created}, Updated: {updated}, Deleted: {deleted}, "
        f"Skipped: {skipped}, Ignored (digital_garden=false): {ignored}"
    )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
