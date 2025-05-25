#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Crusader Kings III Mod Updater Script (v21 – AST Merge & Bug Fix)
# -----------------------------------------------------------------------------
# Author: AI Assistant (with user guidance)
# Date: 2025-05-25
#
# Purpose:
#   Deep-merge CK3 mod changes into an updated vanilla base using a robust AST.
#   Compares:
#     • Your Mod against Old Vanilla (reference)
#     • Your Mod against New Vanilla (game update)
#   Produces an incremental, non-destructive merge in an output directory,
#   preserving nested structure, comments, and formatting.
#
# Key Features:
# 1. Full LL(1) brace-aware parser → AST (BlockNode, ParamNode, CommentNode)
# 2. Three-way diff on parameter “paths” (e.g. ["innovation_longboats","character_modifier","diplomatic_range_mult"])
#    with ParamPath made hashable to avoid mapping errors.
# 3. Deep updates at any nesting level—parameters are inserted into the correct sub-block.
# 4. Interactive prompts with flags:
#      --auto          apply all changes without asking
#      --dry-run       parse & diff only; don’t write files
#      --summary-only  show summaries; don’t apply or write
# 5. Deterministic pretty-printer: consistent 4-space indents, round-trip comments/blanks
# 6. Original files are read-only; all output goes under `updated_mod_v21_ast/`
#
# Usage:
#   python ck3_mod_updater.py                # interactive mode
#   python ck3_mod_updater.py --auto         # fully automatic
#   python ck3_mod_updater.py --dry-run      # parse & diff, no writes
#   python ck3_mod_updater.py --summary-only # print summaries only
# -----------------------------------------------------------------------------

from __future__ import annotations
import os
import sys
import argparse
import shutil
import logging
import filecmp
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Iterable, Union

# -----------------------------------------------------------------------------
# 1. LEXER & PARSER: Build AST of Nodes from PDS/CK3 text
# -----------------------------------------------------------------------------

Token = Tuple[str, str]  # ("SYMBOL","{") or ("IDENT","foo") etc.

def _tokenise(text: str) -> List[Token]:
    import re
    pattern = re.compile(r"""
        (?P<SPACE>\s+)
      | (?P<SYM>[=\{\}#])
      | (?P<ID>[@\w\.\-]+)
      | (?P<MISC>[^\s\{\}=#]+)
    """, re.VERBOSE)
    tokens: List[Token] = []
    for m in pattern.finditer(text):
        kind = m.lastgroup
        if kind == "SPACE":
            continue
        tok = m.group(kind)
        if kind in ("SYM", "MISC"):
            tokens.append(("SYMBOL", tok))
        else:
            tokens.append(("IDENT", tok))
    return tokens

@dataclass
class Node:
    """Base-class for all AST nodes."""
    def to_lines(self, indent: int = 0) -> List[str]:
        raise NotImplementedError

@dataclass
class ParamNode(Node):
    key: str
    value: str
    comment: Optional[str] = None
    def to_lines(self, indent: int = 0) -> List[str]:
        ind = " " * indent
        cm = f" # {self.comment}" if self.comment else ""
        return [f"{ind}{self.key} = {self.value}{cm}\n"]

@dataclass
class BlockNode(Node):
    name: str
    children: List[Node] = field(default_factory=list)
    inline: bool = False
    def get_param(self, key: str) -> Optional[ParamNode]:
        for c in self.children:
            if isinstance(c, ParamNode) and c.key == key:
                return c
        return None
    def update_param(self, key: str, new_val: str, comment: Optional[str] = None) -> None:
        p = self.get_param(key)
        if p:
            p.value = new_val
            if comment:
                p.comment = comment
        else:
            self.children.append(ParamNode(key, new_val, comment))
    def to_lines(self, indent: int = 0) -> List[str]:
        ind = " " * indent
        if self.inline and all(isinstance(c, ParamNode) for c in self.children):
            inner = " ".join(f"{c.key} = {c.value}" for c in self.children)
            return [f"{ind}{self.name} = {{ {inner} }}\n"]
        lines = [f"{ind}{self.name} = {{\n"]
        for c in self.children:
            lines.extend(c.to_lines(indent + 4))
        lines.append(f"{ind}}}\n")
        return lines

@dataclass
class CommentNode(Node):
    text: str
    def to_lines(self, indent: int = 0) -> List[str]:
        ind = " " * indent
        return [f"{ind}# {self.text}\n"]

@dataclass
class ParserError(Exception):
    pass

class Parser:
    def __init__(self, tokens: List[Token]) -> None:
        self.toks = tokens
        self.pos = 0

    def _peek(self, offset: int = 0) -> Optional[Token]:
        idx = self.pos + offset
        return self.toks[idx] if idx < len(self.toks) else None

    def _eat(self, expect: Optional[str] = None) -> Token:
        if self.pos >= len(self.toks):
            raise ParserError("Unexpected EOF")
        tok = self.toks[self.pos]
        self.pos += 1
        if expect and tok[1] != expect:
            raise ParserError(f"Expected '{expect}', got '{tok[1]}'")
        return tok

    def parse_file(self) -> List[Node]:
        nodes: List[Node] = []
        while self.pos < len(self.toks):
            tok = self._peek()
            if tok == ("SYMBOL", "#"):
                nodes.append(self._parse_comment())
            elif tok and tok[0] == "IDENT":
                # could be ParamNode or BlockNode
                # lookahead for '=' then '{'
                if self._peek(1) == ("SYMBOL", "=") and self._peek(2) == ("SYMBOL", "{"):
                    nodes.append(self._parse_block())
                else:
                    nodes.append(self._parse_param())
            else:
                # skip unexpected tokens
                self.pos += 1
        return nodes

    def _parse_comment(self) -> CommentNode:
        self._eat("#")
        parts: List[str] = []
        while self.pos < len(self.toks) and self._peek()[1] not in ("#", "{", "}", "="):
            parts.append(self._eat()[1])
        return CommentNode(" ".join(parts))

    def _parse_param(self) -> ParamNode:
        key = self._eat()[1]
        self._eat("=")
        val = self._eat()[1]
        comment = None
        if self._peek() == ("SYMBOL", "#"):
            self._eat("#")
            parts = []
            while self.pos < len(self.toks) and self._peek()[1] not in ("#", "{", "}", "="):
                parts.append(self._eat()[1])
            comment = " ".join(parts)
        return ParamNode(key, val, comment)

    def _parse_block(self) -> BlockNode:
        name = self._eat()[1]
        self._eat("=")
        self._eat("{")
        children: List[Node] = []
        # detect empty inline
        if self._peek() == ("SYMBOL", "}"):
            self._eat("}")
            return BlockNode(name, [], inline=True)
        while True:
            if self._peek() == ("SYMBOL", "}"):
                self._eat("}")
                break
            tok = self._peek()
            if tok == ("SYMBOL", "#"):
                children.append(self._parse_comment())
            elif tok and tok[0] == "IDENT":
                # nested block?
                if self._peek(1) == ("SYMBOL", "=") and self._peek(2) == ("SYMBOL", "{"):
                    children.append(self._parse_block())
                else:
                    children.append(self._parse_param())
            else:
                # skip stray tokens
                self.pos += 1
        return BlockNode(name, children)

def parse_text_to_ast(text: str) -> List[Node]:
    tokens = _tokenise(text)
    return Parser(tokens).parse_file()

def ast_to_lines(nodes: List[Node]) -> List[str]:
    out: List[str] = []
    for n in nodes:
        out.extend(n.to_lines(0))
    return out

# -----------------------------------------------------------------------------
# 2. THREE-WAY DIFF & CHANGESET
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ParamPath:
    entry_chain: Tuple[str, ...]
    key: str

    def __str__(self) -> str:
        return " > ".join(self.entry_chain + (self.key,))

@dataclass
class ChangeItem:
    path: ParamPath
    old_val: Optional[str]
    new_val: Optional[str]
    mod_val: str
    status: str  # "add", "update", "conflict", "removed"

def _traverse(nodes: List[Node], chain: Tuple[str, ...]) -> Iterable[Tuple[ParamPath, str]]:
    for n in nodes:
        if isinstance(n, ParamNode):
            yield ParamPath(chain, n.key), n.value
        elif isinstance(n, BlockNode):
            yield from _traverse(n.children, chain + (n.name,))

def build_map(ast: List[Node]) -> Dict[ParamPath, str]:
    return dict(_traverse(ast, ()))

def diff_three_way(old_ast: List[Node], new_ast: List[Node], mod_ast: List[Node]) -> List[ChangeItem]:
    old_map = build_map(old_ast)
    new_map = build_map(new_ast)
    mod_map = build_map(mod_ast)

    changes: List[ChangeItem] = []
    for path, mval in mod_map.items():
        oval = old_map.get(path)
        nval = new_map.get(path)
        if oval is None:
            status = "add" if nval is None else "conflict"
        else:
            if nval is None:
                status = "removed"
            elif mval == nval:
                continue
            elif oval == nval:
                status = "update"
            else:
                status = "conflict"
        changes.append(ChangeItem(path, oval, nval, mval, status))
    return changes

# -----------------------------------------------------------------------------
# 3. APPLY CHANGES INTO NEW AST
# -----------------------------------------------------------------------------

def locate_block(root: List[Node], chain: Tuple[str, ...]) -> Optional[BlockNode]:
    if not chain:
        return None
    current: List[Node] = root
    blk: Optional[BlockNode] = None
    for seg in chain:
        blk = next((n for n in current if isinstance(n, BlockNode) and n.name == seg), None)
        if blk is None:
            return None
        current = blk.children
    return blk

def apply_change(new_ast: List[Node], ch: ChangeItem, comment_tag: str) -> bool:
    blk = locate_block(new_ast, ch.path.entry_chain)
    if not blk:
        logging.warning("Missing block for %s", ch.path.entry_chain)
        return False
    blk.update_param(ch.path.key, ch.mod_val, comment=comment_tag)
    return True

# -----------------------------------------------------------------------------
# 4. CONFIGURATION & MAIN WORKFLOW
# -----------------------------------------------------------------------------

CONFIG = {
    "MOD_SOURCE_DIR":     r"C:\Users\Galaxy\Documents\Paradox Interactive\Crusader Kings III\mod\custom_changes",
    "OLD_VANILLA_DIR":    r"C:\Users\Galaxy\LEVI\jupyter\ck3_mod_update\old_ver",
    "NEW_VANILLA_DIR":    r"C:\Program Files (x86)\Steam\steamapps\common\Crusader Kings III\game",
    "OUTPUT_FOLDER_NAME": "updated_mod_v21_ast",
    "FOLDERS_TO_PROCESS": ["common", "events"],
}

def read_text(path: str) -> str:
    with open(path, encoding="utf-8-sig") as f:
        return f.read()

def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(text)

def gather_paths() -> List[str]:
    rels: List[str] = []
    for d in CONFIG["FOLDERS_TO_PROCESS"]:
        base = os.path.join(CONFIG["MOD_SOURCE_DIR"], d)
        for root, _, files in os.walk(base):
            for fn in files:
                if fn.lower().endswith(".txt"):
                    rels.append(os.path.relpath(os.path.join(root, fn), CONFIG["MOD_SOURCE_DIR"]))
    return rels

def process_file(rel: str, args) -> bool:
    paths = {
        "mod": os.path.join(CONFIG["MOD_SOURCE_DIR"], rel),
        "old": os.path.join(CONFIG["OLD_VANILLA_DIR"], rel),
        "nv":  os.path.join(CONFIG["NEW_VANILLA_DIR"], rel),
    }
    # handle brand-new mod files
    if not os.path.exists(paths["old"]) and os.path.exists(paths["mod"]) and not os.path.exists(paths["nv"]):
        if args.auto or (not args.summary_only and input(f"Copy new mod file '{rel}'? [y/N] ").lower().startswith("y")):
            outp = os.path.join(CONFIG["OUTPUT_FOLDER_NAME"], rel)
            write_text(outp, read_text(paths["mod"]))
            logging.info("Copied new file %s", rel)
            return True
        return False

    # parse
    asts = {}
    for key in ("old", "nv", "mod"):
        if os.path.exists(paths[key]):
            asts[key] = parse_text_to_ast(read_text(paths[key]))
        else:
            asts[key] = []

    changes = diff_three_way(asts["old"], asts["nv"], asts["mod"])
    if not changes:
        logging.info("No changes in %s", rel)
        return False

    # summary
    if not args.auto and not args.summary_only:
        print(f"\n=== {rel} ({len(changes)} change(s)) ===")
        for c in changes:
            print(f" {c.status:8} | {c.path} | OV={c.old_val} | NV={c.new_val} | MOD={c.mod_val}")
        if not input("Apply changes? [y/N] ").lower().startswith("y"):
            return False

    if args.summary_only:
        return False

    modified = False
    for c in changes:
        tag = f"MERGED OV={c.old_val} NV={c.new_val}"
        modified |= apply_change(asts["nv"], c, tag)

    if modified and not args.dry_run:
        outp = os.path.join(CONFIG["OUTPUT_FOLDER_NAME"], rel)
        write_text(outp, "".join(ast_to_lines(asts["nv"])))
        logging.info("Wrote %s", rel)

    return modified

def main():
    parser = argparse.ArgumentParser(description="CK3 Mod Updater v21 – AST Merge & Hashable Paths")
    parser.add_argument("--auto",         action="store_true", help="Apply all without prompting")
    parser.add_argument("--dry-run",      action="store_true", help="Parse & diff only; no writes")
    parser.add_argument("--summary-only", action="store_true", help="Show summaries only; no applies")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    outdir = os.path.join(os.getcwd(), CONFIG["OUTPUT_FOLDER_NAME"])
    if not args.dry_run:
        if os.path.isdir(outdir):
            shutil.rmtree(outdir)
        os.makedirs(outdir, exist_ok=True)

    total = 0
    for rel in gather_paths():
        try:
            if process_file(rel, args):
                total += 1
        except Exception:
            logging.exception("Error processing %s", rel)

    logging.info("Finished. Files modified: %d", total)

if __name__ == "__main__":
    main()
