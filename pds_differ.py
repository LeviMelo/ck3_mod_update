# pds_differ.py
# Performs a 3-way diff and simulates a merge for Paradox Script (PDS) files.
# Contains:
#   - PdsChange: Represents a detected difference between Old, Mod, and New versions
#                of a PDS node/structure. Types include MOD_ADDED, VANILLA_MODIFIED,
#                CONFLICT_MODIFIED, etc.
#   - PairwiseDiffResult: Represents a difference from a 2-way comparison.
#   - PdsDiffer:
#     - `_get_node_key_for_path`: Generates a unique-ish string key for a node
#       to aid in path construction during diffing.
#     - `_are_nodes_structurally_equal`: Compares two PdsNodes for structural equality.
#     - `_perform_pairwise_diff`: Uses difflib.SequenceMatcher to compare two lists
#       of PdsNodes (e.g., Old vs. Mod, Old vs. New), producing PairwiseDiffResult objects.
#       Recurses into modified blocks.
#     - `diff_nodes`: Orchestrates the 3-way diff. It calls `_perform_pairwise_diff`
#       for (Old, Mod) and (Old, New), then reconciles these into a list of PdsChange
#       objects representing the three-way differences and conflicts.
#     - `simulate_three_way_merge`: Takes the list of PdsChange objects and a copy
#       of the New vanilla AST. It attempts to apply the Mod's changes onto the
#       New vanilla AST, respecting Vanilla's changes where possible and resolving
#       conflicts according to defined heuristics.
#   - Helper functions for AST navigation and modification during simulation.
#   - `_apply_single_change_to_sim_tree`: Core logic for applying one PdsChange
#     to the simulated AST.
#
# Current Known Issues/Limitations:
#   - Diffing & Reconciliation (`diff_nodes`):
#     - Path generation for added items (`___INSERTED_`) can be complex.
#     - Reconciliation logic for `f_type` is extensive and may have edge cases
#       leading to "ERROR_" or "UNHANDLED" types.
#   - Merge Simulation (`simulate_three_way_merge` / `_apply_single_change_to_sim_tree`):
#     - Conflict Resolution:
#       - `CONFLICT_MODIFIED` on a *Block*: The critical change made is to *not* replace
#         the entire block with the mod's version. Instead, the vanilla block structure
#         is kept, commented, and child-level diffs are allowed to proceed. This aims
#         to merge contents rather than pick one block wholesale. This was the primary
#         fix for the `innovation_longboats` issue where vanilla additions were lost.
#       - `CONFLICT_ADDITION`: Mod's addition is typically appended. Order might need refinement.
#     - Node Finding (`_find_node_and_parent_in_sim_tree`): Relies on object identity for
#       copied nodes and path-based finding. Tree modifications can make this tricky.
#     - Subsumption Logic (`g_subsumed_mod_block_paths_for_simulation`):
#       Prevents applying child changes within a block that was *entirely replaced by*
#       or *newly added by* the mod. A block whose *contents are merged* (like a
#       `CONFLICT_MODIFIED` block handled by the new strategy) should NOT have its path subsumed,
#       allowing its children to be merged.
#   - Idempotency: Not guaranteed, especially with comment additions.
#   - Order of Changes: Applied as received. Sorting might be beneficial but is complex.
import difflib
import sys
import collections # For deque in find_copied_node_recursive
import re

# Import PdsNode types from pds_parser
from pds_parser import (
    PdsNode, PdsBlock, PdsComment, PdsBlankLine, 
    PdsKeyValuePair, PdsList, PdsOperatorCondition
)

# --- PdsChange and PairwiseDiffResult (UNCHANGED) ---
class PdsChange:
    def __init__(self, type, key_path, old_node=None, mod_node=None, new_node=None, context_parent_path=None):
        self.type = type; self.key_path = key_path; self.old_node = old_node; self.mod_node = mod_node
        self.new_node = new_node; self.context_parent_path = context_parent_path if context_parent_path is not None else []
    def __repr__(self):
        def _get_node_short_repr(node):
            if node is None: return "ABSENT"
            s = repr(node); return s[1:-1] if len(s) <= 70 else s[1:34] + "..." + s[-34:-1]
        o=f"O:{_get_node_short_repr(self.old_node)}";m=f"M:{_get_node_short_repr(self.mod_node)}";n=f"N:{_get_node_short_repr(self.new_node)}"
        p='.'.join(map(str,self.key_path)) if self.key_path else 'ROOT';pp='.'.join(map(str,self.context_parent_path)) if self.context_parent_path else 'ROOT_PARENT'
        return f"PdsChange(Type='{self.type:<45}', Path='{p}', \n          ParentCtx='{pp}', \n          Nodes=[\n            {o},\n            {m},\n            {n}\n          ])"

class PairwiseDiffResult:
    def __init__(self, change_type, path, old_item=None, new_item=None, old_idx=-1, new_idx=-1):
        self.change_type=change_type; self.path=path; self.old_item=old_item; self.new_item=new_item
        self.old_idx=old_idx; self.new_idx=new_idx
    def __repr__(self):
        o=f"'{getattr(self.old_item,'key',type(self.old_item).__name__)}'" if self.old_item else "None"
        n=f"'{getattr(self.new_item,'key',type(self.new_item).__name__)}'" if self.new_item else "None"
        return f"PairwiseDiff(type={self.change_type}, path={'.'.join(self.path)}, old={o}@{self.old_idx}, new={n}@{self.new_idx})"

# --- Module-level state for simulation (reset per call) ---
g_subsumed_mod_block_paths_for_simulation = set()

# --- Helper functions for navigation and node finding (UNCHANGED from previous good version) ---
def _parse_indexed_identifier_for_nav(identifier_full):
    if isinstance(identifier_full, str) and "___" in identifier_full:
        parts = identifier_full.split("___", 1); base_key = parts[0]
        try: index = int(parts[1]); return base_key, index, True
        except ValueError: return identifier_full, 0, False 
    return str(identifier_full) if identifier_full is not None else None, 0, False

def _get_key_from_path_segment(segment_str: str) -> str:
    if "___" in segment_str: return segment_str.split("___", 1)[0]
    return segment_str

def _find_container_by_key_path(root_items_or_block_node, key_name_path: list[str]):
    current_container = root_items_or_block_node
    for i, key_name in enumerate(key_name_path):
        found_next = None; search_in = []
        if isinstance(current_container, list): search_in = current_container
        elif isinstance(current_container, PdsBlock): search_in = current_container.children
        else: return None
        for item in search_in:
            if isinstance(item, PdsBlock) and hasattr(item, 'key') and item.key == key_name: found_next = item; break
            elif isinstance(item, PdsKeyValuePair) and hasattr(item, 'key') and item.key == key_name and isinstance(item.value, PdsBlock): found_next = item.value; break
        if not found_next: return None 
        current_container = found_next
        if i < len(key_name_path) - 1 and not isinstance(current_container, PdsBlock): return None
    return current_container if isinstance(current_container, (PdsBlock, list)) else None

def _get_node_diff_key_for_find(node: PdsNode): 
    if node is None: return "__NONE__"
    if hasattr(node, 'key') and node.key is not None:
        key_str = str(node.key); return key_str.replace("___", "__^__").replace(".", "^")
    if isinstance(node, PdsComment): return f"__COMMENT_{hash(node.comment_text[:20])}"
    if isinstance(node, PdsBlankLine): return f"__BLANK_LINE_{node.line_number}"
    return f"__{node.__class__.__name__.upper()}_L{node.line_number}"

def _find_node_by_diff_path_in_tree(current_level_items: list, diff_path_list: list[str]):
    target_node = None
    for seg_idx, seg_str in enumerate(diff_path_list):
        if "___INSERTED_at_" in seg_str and seg_idx == len(diff_path_list) -1 : return None
        key_part, occ_idx, is_idxed = _parse_indexed_identifier_for_nav(seg_str)
        key_match_count = 0; found_this_level_node = False; search_list = []
        if isinstance(current_level_items, list): search_list = current_level_items
        elif isinstance(current_level_items, PdsBlock): search_list = current_level_items.children
        else: return None 
        for node_in_current_level in search_list:
            sim_key_str = _get_node_diff_key_for_find(node_in_current_level)
            if sim_key_str == key_part:
                if not is_idxed or key_match_count == occ_idx:
                    target_node = node_in_current_level; found_this_level_node = True; break
                key_match_count +=1
        if not found_this_level_node: return None 
        if seg_idx < len(diff_path_list)-1: 
            if isinstance(target_node, PdsBlock): current_level_items = target_node 
            else: return None
    return target_node

def _find_copied_node_recursive(sim_items_root_list: list, original_node_to_find: PdsNode):
    if original_node_to_find is None: return None, None 
    q = collections.deque()
    for item in sim_items_root_list: q.append((item, sim_items_root_list)) 
    while q:
        current_node, parent_collection_or_block = q.popleft()
        if current_node == original_node_to_find: return current_node, parent_collection_or_block
        if isinstance(current_node, PdsBlock):
            for child in current_node.children: q.append((child, current_node)) 
        elif isinstance(current_node, (PdsKeyValuePair, PdsOperatorCondition)) and isinstance(current_node.value, PdsBlock):
            block_value = current_node.value
            for child_in_value_block in block_value.children: q.append((child_in_value_block, block_value)) 
        elif isinstance(current_node, PdsList):
            for value_item in current_node.values:
                if isinstance(value_item, PdsBlock):
                    for child_in_list_block in value_item.children: q.append((child_in_list_block, value_item))
    return None, None 

def _find_node_and_parent_in_sim_tree(sim_root_list: list, chg_obj: PdsChange):
    target_node_in_sim, parent_collection_in_sim = None, None 
    if chg_obj.type == 'MOD_ADDED' or (chg_obj.type == 'CONFLICT_ADDITION' and chg_obj.mod_node and not chg_obj.old_node): 
        parent_keys_for_path = [_get_key_from_path_segment(s) for s in chg_obj.context_parent_path]
        if not parent_keys_for_path: parent_collection_in_sim = sim_root_list
        else: parent_collection_in_sim = _find_container_by_key_path(sim_root_list, parent_keys_for_path)
        return None, parent_collection_in_sim 
    if chg_obj.new_node is not None: 
        target_node_in_sim, parent_collection_in_sim = _find_copied_node_recursive(sim_root_list, chg_obj.new_node)
        if target_node_in_sim: return target_node_in_sim, parent_collection_in_sim
    if chg_obj.old_node is not None or (chg_obj.key_path and not target_node_in_sim): 
        if not chg_obj.context_parent_path: parent_collection_in_sim = sim_root_list
        else:
            parent_collection_in_sim = _find_node_by_diff_path_in_tree(sim_root_list, chg_obj.context_parent_path)
            if parent_collection_in_sim and not isinstance(parent_collection_in_sim, (list, PdsBlock)): 
                parent_collection_in_sim = None
        if parent_collection_in_sim is not None and chg_obj.key_path:
            last_segment_path = [chg_obj.key_path[-1]]; items_to_search_target_in = []
            if isinstance(parent_collection_in_sim, list): items_to_search_target_in = parent_collection_in_sim
            elif isinstance(parent_collection_in_sim, PdsBlock): items_to_search_target_in = parent_collection_in_sim.children
            target_node_in_sim = _find_node_by_diff_path_in_tree(items_to_search_target_in, last_segment_path)
    if isinstance(parent_collection_in_sim, PdsBlock) and target_node_in_sim and target_node_in_sim not in parent_collection_in_sim.children:
        parent_collection_in_sim = None 
    if parent_collection_in_sim is not None and not isinstance(parent_collection_in_sim, (list, PdsBlock)):
        parent_collection_in_sim = None 
    return target_node_in_sim, parent_collection_in_sim

# --- Helper for adding simulation comments (UNCHANGED) ---
def _sim_add_comment_to_node(node_to_comment, comment_text):
    if node_to_comment is None: return
    if isinstance(node_to_comment, PdsComment): return 
    if hasattr(node_to_comment, 'comment_text_on_line'):
        current_comment = getattr(node_to_comment, 'comment_text_on_line', None)
        node_to_comment.comment_text_on_line = f"{current_comment} {comment_text}" if current_comment else comment_text
    elif isinstance(node_to_comment, PdsBlock): 
        comment_node = PdsComment(comment_text)
        parent_indent = node_to_comment.indent_level if node_to_comment.indent_level is not None else 0
        comment_node.indent_level = parent_indent + 4 
        node_to_comment.children.insert(0, comment_node)

# --- Core Simulation Logic ---
def _apply_single_change_to_sim_tree(sim_tree_root_list: list, chg_obj: PdsChange, subsumed_paths_ref: set):
    current_path_tuple = tuple(chg_obj.key_path)

    for subsuming_path_tuple_prefix in subsumed_paths_ref:
        if len(current_path_tuple) > len(subsuming_path_tuple_prefix) and \
           current_path_tuple[:len(subsuming_path_tuple_prefix)] == subsuming_path_tuple_prefix:
            # print(f"  Skipping subsumed MOD change: {chg_obj.type} at {'->'.join(chg_obj.key_path)}") # Verbose skip
            return

    # print(f"\n  Attempting to apply change: {chg_obj.type} at OldPath {'->'.join(chg_obj.key_path)}") # Verbose apply
    sim_comment_text = f"SimMerge:{chg_obj.type}"
    
    target_node_in_sim, parent_in_sim = _find_node_and_parent_in_sim_tree(sim_tree_root_list, chg_obj)
    debug_path_str = '->'.join(chg_obj.key_path) if chg_obj.key_path else "ROOT"

    involved_node_type = None
    # Determine the type of node this change primarily concerns itself with (old, mod, or new version)
    # This helps decide if it's a block-level change or item-level change.
    if chg_obj.type.endswith('_ADDED') and chg_obj.mod_node: involved_node_type = type(chg_obj.mod_node)
    elif chg_obj.type.endswith('_ADDED') and chg_obj.new_node: involved_node_type = type(chg_obj.new_node)
    elif chg_obj.old_node: involved_node_type = type(chg_obj.old_node)
    elif chg_obj.mod_node: involved_node_type = type(chg_obj.mod_node) # Fallback if old_node is gone but mod exists
    elif chg_obj.new_node: involved_node_type = type(chg_obj.new_node) # Fallback if old/mod gone but new exists (e.g. VANILLA_DELETED)

    # --- Apply Merge Logic ---
    if chg_obj.type == 'MOD_MODIFIED':
        if involved_node_type in (PdsKeyValuePair, PdsList, PdsOperatorCondition, PdsComment, PdsBlankLine):
            if target_node_in_sim and parent_in_sim and chg_obj.mod_node:
                mod_node_copy = chg_obj.mod_node.copy(); _sim_add_comment_to_node(mod_node_copy, sim_comment_text)
                replaced = False
                if isinstance(parent_in_sim, list):
                    try: idx = parent_in_sim.index(target_node_in_sim); parent_in_sim[idx] = mod_node_copy; replaced = True
                    except ValueError: pass 
                elif isinstance(parent_in_sim, PdsBlock): replaced = parent_in_sim.replace_child(target_node_in_sim, mod_node_copy)
                if replaced: print(f"      Applied MOD_MODIFIED non-Block. Path: {debug_path_str}")
                else: print(f"    SIM MERGE ERROR (MOD_MODIFIED non-Block): Replacement failed. Path: {debug_path_str}")
            else: print(f"    SIM MERGE FAIL (MOD_MODIFIED non-Block): Missing elements. Path: {debug_path_str} (Tgt:{target_node_in_sim is not None}, Prnt:{parent_in_sim is not None}, ModN:{chg_obj.mod_node is not None})")
        elif involved_node_type == PdsBlock:
             if target_node_in_sim: _sim_add_comment_to_node(target_node_in_sim, sim_comment_text + "_CONTENTS_MERGED_BY_CHILDREN"); print(f"      Commented MOD_MODIFIED Block. Path: {debug_path_str}")
             else: print(f"    SIM MERGE WARN (MOD_MODIFIED Block): Target block not found to comment. Path: {debug_path_str}")
        else: print(f"    SIM MERGE NOTE (MOD_MODIFIED): Unhandled node type {involved_node_type}. Path: {debug_path_str}")

    elif chg_obj.type == 'MOD_ADDED':
         if parent_in_sim is not None and chg_obj.mod_node:
             mod_node_copy = chg_obj.mod_node.copy(); _sim_add_comment_to_node(mod_node_copy, sim_comment_text)
             added = False
             if isinstance(parent_in_sim, list): parent_in_sim.append(mod_node_copy); added = True
             elif isinstance(parent_in_sim, PdsBlock): added = parent_in_sim.add_child_at_appropriate_location(mod_node_copy)
             else: print(f"    SIM MERGE ERROR (MOD_ADDED): Invalid parent type {type(parent_in_sim).__name__}. Path: {debug_path_str}"); return
             if added:
                 print(f"      Applied MOD_ADDED. Path: {debug_path_str}")
                 if isinstance(mod_node_copy, PdsBlock): subsumed_paths_ref.add(current_path_tuple) # Mod added this block, its contents are from mod.
             else: print(f"    SIM MERGE ERROR (MOD_ADDED): Add failed. Path: {debug_path_str}")
         else: print(f"    SIM MERGE FAIL (MOD_ADDED): Missing parent_in_sim ({parent_in_sim is not None}) or mod_node ({chg_obj.mod_node is not None}). Path: {debug_path_str}")

    elif chg_obj.type == 'MOD_DELETED':
        if target_node_in_sim and parent_in_sim is not None:
            removed = False
            if isinstance(parent_in_sim, list):
                try: parent_in_sim.remove(target_node_in_sim); removed = True
                except ValueError: pass 
            elif isinstance(parent_in_sim, PdsBlock): removed = parent_in_sim.remove_child(target_node_in_sim)
            if removed: print(f"      Applied MOD_DELETED. Path: {debug_path_str}")
            else: print(f"    SIM MERGE ERROR (MOD_DELETED): Removal failed. Path: {debug_path_str}")
        elif not target_node_in_sim : print(f"      MOD_DELETED: Target node already absent. Path: {debug_path_str}")
        else: print(f"    SIM MERGE FAIL (MOD_DELETED): Missing parent for existing target. Path: {debug_path_str}")

    elif chg_obj.type in ('VANILLA_MODIFIED', 'VANILLA_ADDED', 'CONVERGED_MODIFICATION', 'MOD_ADDED_CONVERGED'):
        # These items should already be in the sim_tree (copied from new_nodes). Just comment them.
        if target_node_in_sim:
            _sim_add_comment_to_node(target_node_in_sim, sim_comment_text)
            print(f"      Commented for {chg_obj.type}. Path: {debug_path_str}")
        else: 
            # If a VANILLA_ADDED item is not found, it's an issue unless its parent was deleted by a MOD change.
            # For VANILLA_MODIFIED, target_node_in_sim should exist.
            print(f"    SIM MERGE WARN ({chg_obj.type}): Target node not found to comment. Path: {debug_path_str}. This might be ok if parent was deleted by Mod.")
            
    elif chg_obj.type in ('VANILLA_DELETED', 'MOD_DELETED_VANILLA_ALSO_DELETED'):
        # Node is correctly absent from sim_tree (as it started from new_nodes where it was already deleted).
        print(f"      Noted {chg_obj.type} (node correctly absent). Path: {debug_path_str}")

    elif chg_obj.type == 'CONFLICT_MODIFIED':
        if involved_node_type in (PdsKeyValuePair, PdsList, PdsOperatorCondition, PdsComment, PdsBlankLine):
            # For non-blocks, Mod's version wins the conflict and replaces Vanilla's.
            if target_node_in_sim and parent_in_sim is not None and chg_obj.mod_node:
                mod_node_copy = chg_obj.mod_node.copy(); _sim_add_comment_to_node(mod_node_copy, sim_comment_text + "_MOD_CHOSEN")
                replaced = False
                if isinstance(parent_in_sim, list):
                    try: idx=parent_in_sim.index(target_node_in_sim); parent_in_sim[idx]=mod_node_copy; replaced=True
                    except ValueError:pass
                elif isinstance(parent_in_sim, PdsBlock): replaced = parent_in_sim.replace_child(target_node_in_sim, mod_node_copy)
                if replaced:
                    print(f"        Resolved CONFLICT_MODIFIED non-Block: Replaced with Mod's version. Path: {debug_path_str}")
                    # If this non-block that mod chose IS a block (e.g. KVP value is block), then subsume.
                    if isinstance(mod_node_copy, PdsBlock) or (hasattr(mod_node_copy, 'value') and isinstance(mod_node_copy.value, PdsBlock)):
                         subsumed_paths_ref.add(current_path_tuple)
                else: print(f"      SIM MERGE CONFLICT ERROR ({chg_obj.type} non-Block): Replacement failed. Path: {debug_path_str}")
            else: print(f"      SIM MERGE CONFLICT FAIL ({chg_obj.type} non-Block): Missing elements. Path: {debug_path_str}")
        elif involved_node_type == PdsBlock:
            # For Blocks, DO NOT replace. Keep Vanilla's block structure.
            # Comment the block to indicate conflict and that contents are merged.
            # Child diffs will handle actual content changes. DO NOT SUBSUME this path.
            if target_node_in_sim:
                _sim_add_comment_to_node(target_node_in_sim, sim_comment_text + "_CONTENTS_MERGED_BY_CHILDREN")
                print(f"        Handled CONFLICT_MODIFIED Block: Kept Vanilla block, contents to be merged by children diffs. Path: {debug_path_str}")
            else:
                # This case is problematic. If it's a block conflict, target_node_in_sim (vanilla's block) should exist.
                # If it doesn't, it implies a higher-level deletion might have removed it, or finding failed.
                print(f"      SIM MERGE CONFLICT WARN ({chg_obj.type} Block): Target Vanilla block not found. Mod's block may be lost or incorrectly added if children try. Path: {debug_path_str}")
                # Potentially, if mod_node exists, we could add it here, but that deviates from "keep vanilla structure".
                # This situation implies the vanilla block this conflict refers to was already removed from sim_tree.
                # If chg_obj.mod_node:
                #     if parent_in_sim:
                #         mod_block_copy = chg_obj.mod_node.copy()
                #         _sim_add_comment_to_node(mod_block_copy, sim_comment_text + "_MOD_BLOCK_ADDED_AS_CONFLICT_RESOLUTION")
                #         if isinstance(parent_in_sim, list): parent_in_sim.append(mod_block_copy)
                #         elif isinstance(parent_in_sim, PdsBlock): parent_in_sim.add_child_at_appropriate_location(mod_block_copy)
                #         subsumed_paths_ref.add(current_path_tuple) # Since we added mod's block
                #         print(f"        Added Mod's block as conflict resolution fallback. Path: {debug_path_str}")

        else: print(f"    SIM MERGE NOTE (CONFLICT_MODIFIED): Unhandled node type {involved_node_type}. Path: {debug_path_str}")


    elif chg_obj.type == 'CONFLICT_ADDITION':
         # Both Mod and Vanilla added something at a similar conceptual location relative to Old.
         # Vanilla's addition (chg_obj.new_node / target_node_in_sim) is already in the sim_tree.
         # We need to add Mod's addition (chg_obj.mod_node) as well.
         if parent_in_sim is not None and chg_obj.mod_node:
            mod_node_copy = chg_obj.mod_node.copy()
            _sim_add_comment_to_node(mod_node_copy, sim_comment_text + "_MOD_ALSO_ADDED")
            
            added = False
            # Prefer adding Mod's node after Vanilla's if Vanilla's node is identifiable (target_node_in_sim)
            # Otherwise, just add it to the parent.
            if isinstance(parent_in_sim, PdsBlock):
                added = parent_in_sim.add_child_at_appropriate_location(mod_node_copy, after_node_instance=target_node_in_sim if target_node_in_sim in parent_in_sim.children else None)
            elif isinstance(parent_in_sim, list):
                if target_node_in_sim and target_node_in_sim in parent_in_sim:
                    try:
                        idx = parent_in_sim.index(target_node_in_sim)
                        parent_in_sim.insert(idx + 1, mod_node_copy)
                        added = True
                    except ValueError: # target_node_in_sim not in parent_in_sim list
                        parent_in_sim.append(mod_node_copy)
                        added = True
                else: # target_node_in_sim not found or not applicable, just append
                    parent_in_sim.append(mod_node_copy)
                    added = True
            
            if added: 
                print(f"        Resolved CONFLICT_ADDITION: Applied Mod's addition. Path: {debug_path_str} (Mod node key: {getattr(mod_node_copy, 'key', 'N/A')})")
                # If mod added a block, its path should be subsumed as its content is entirely from mod.
                if isinstance(mod_node_copy, PdsBlock):
                    # The path for this added node from mod is typically ...KEY___INSERTED...
                    # So, current_path_tuple for the CONFLICT_ADDITION itself might be the shared "conceptual location"
                    # The actual path of the *mod's added node* would be distinct.
                    # For subsumption, we need the path of the node *that was added*.
                    # This is tricky because chg_obj.key_path for CONFLICT_ADDITION might not be specific to mod's node.
                    # Let's assume for now that if mod adds a block in a conflict, its internal structure is from mod.
                    # We need a robust way to get the *effective path* of the newly added mod_node_copy if it's a block.
                    # For now, let's be conservative and not subsume here unless we can clearly identify mod's new block path.
                    pass # subsumed_paths_ref.add( PATH_OF_MOD_NODE_COPY ) 
            else: print(f"      SIM MERGE CONFLICT ERROR ({chg_obj.type}): Add failed. Path: {debug_path_str}")
         else: print(f"      SIM MERGE CONFLICT FAIL ({chg_obj.type}): Missing parent ({parent_in_sim is not None}) or mod_node ({chg_obj.mod_node is not None}). Path: {debug_path_str}")

    elif chg_obj.type == 'CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED':
        # Mod deleted it, Vanilla modified it. Keep Vanilla's modified version (already in sim tree).
        if target_node_in_sim: # Vanilla's modified node should be in sim_tree
             _sim_add_comment_to_node(target_node_in_sim, sim_comment_text + "_VANILLA_MOD_KEPT")
             print(f"        Resolved {chg_obj.type}: Kept Vanilla's modified version. Path: {debug_path_str}")
        else: print(f"      SIM MERGE CONFLICT WARN ({chg_obj.type}): Vanilla's modified node (target) not found. Path: {debug_path_str}")

    elif chg_obj.type == 'CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED':
         # Vanilla deleted it, Mod modified it. Add Mod's modified version.
         if parent_in_sim and chg_obj.mod_node:
             mod_node_copy = chg_obj.mod_node.copy(); _sim_add_comment_to_node(mod_node_copy, sim_comment_text + "_MOD_MOD_APPLIED_AS_ADD")
             added = False
             if isinstance(parent_in_sim, list): parent_in_sim.append(mod_node_copy); added = True
             elif isinstance(parent_in_sim, PdsBlock): added = parent_in_sim.add_child_at_appropriate_location(mod_node_copy); added = True
             if added:
                 print(f"        Resolved {chg_obj.type}: Applied Mod's modified version (as add). Path: {debug_path_str}")
                 if isinstance(mod_node_copy, PdsBlock): subsumed_paths_ref.add(current_path_tuple) # Mod's block structure added.
             else: print(f"      SIM MERGE CONFLICT ERROR ({chg_obj.type}): Add failed. Path: {debug_path_str}")
         else: print(f"      SIM MERGE CONFLICT FAIL ({chg_obj.type}): Missing elements. Path: {debug_path_str}")
    
    elif chg_obj.type.startswith("ERROR_") or chg_obj.type == "UNHANDLED_RECONCILIATION_CASE":
        print(f"    SIM MERGE ERROR: Unhandled differ type: {chg_obj.type}. Path: {debug_path_str}")
    
class PdsDiffer:
    # --- Diffing logic (UNCHANGED) ---
    def _get_node_key_for_path(self, node: PdsNode):
        if node is None: return "__NONE__"
        if hasattr(node, 'key') and node.key is not None:
            key_str = str(node.key); return key_str.replace("___", "__^__").replace(".", "^")
        if isinstance(node, PdsComment): return f"__COMMENT_{hash(node.comment_text[:20])}"
        if isinstance(node, PdsBlankLine): return f"__BLANK_LINE_{node.line_number}"
        return f"__{node.__class__.__name__.upper()}_L{node.line_number}"

    def _are_nodes_structurally_equal(self, node1: PdsNode, node2: PdsNode):
        if node1 is None and node2 is None: return True
        if node1 is None or node2 is None: return False
        return node1 == node2 

    def _perform_pairwise_diff(self, list1_nodes: list[PdsNode], list2_nodes: list[PdsNode], current_path_prefix: list[str]):
        pairwise_changes = []
        # Using full structural components for SequenceMatcher to better align complex nodes initially
        seq_match_items1 = [node.get_comparator_key(shallow_block_for_seq_matcher=False) for node in list1_nodes]
        seq_match_items2 = [node.get_comparator_key(shallow_block_for_seq_matcher=False) for node in list2_nodes]
        
        sm = difflib.SequenceMatcher(None, seq_match_items1, seq_match_items2, autojunk=False)
        key_counts_list1 = {} 
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                for idx_offset in range(i2 - i1):
                    l1_idx, l2_idx = i1 + idx_offset, j1 + idx_offset
                    n1, n2 = list1_nodes[l1_idx], list2_nodes[l2_idx]
                    n1_key = self._get_node_key_for_path(n1) # Path based on n1's key
                    occurrence = key_counts_list1.get(n1_key, 0); key_counts_list1[n1_key] = occurrence + 1
                    path_seg = f"{n1_key}___{occurrence}"
                    item_path = current_path_prefix + [path_seg]
                    
                    # Since SequenceMatcher used full get_comparator_key, 'equal' tag means they are structurally identical.
                    change_type = 'IDENTICAL' 
                    pairwise_changes.append(PairwiseDiffResult(change_type, item_path, n1, n2, l1_idx, l2_idx))
                    # No need to recurse if IDENTICAL, as full get_comparator_key already confirmed deep equality.

            elif tag == 'replace': 
                # If SequenceMatcher says 'replace' with full structural comparison, it means nodes are different.
                # We need to check if they are superficially similar (e.g. same key, different content)
                # vs. completely different items.
                
                # This loop handles the case where multiple items in list1 are replaced by multiple in list2
                # For a more precise MODIFIED vs ADD/DELETE, we might need finer-grained comparison here.
                # Current logic: delete all from list1, add all from list2 in the 'replace' block.
                
                # Heuristic: if same number of items replaced, consider them MODIFIED pairwise if keys match
                if (i2 - i1) == (j2 - j1): # Same number of items in old and new range
                    is_all_modified = True
                    temp_pairwise_for_replace = []
                    temp_key_counts_list1_for_replace = key_counts_list1.copy()

                    for k_offset in range(i2 - i1):
                        l1_idx, l2_idx = i1 + k_offset, j1 + k_offset
                        n1, n2 = list1_nodes[l1_idx], list2_nodes[l2_idx]
                        n1_key = self._get_node_key_for_path(n1)
                        
                        # Check if n1 and n2 are "conceptually" the same item but modified (e.g. same key)
                        if self._get_node_key_for_path(n1) == self._get_node_key_for_path(n2) or \
                           (hasattr(n1, 'key') and hasattr(n2, 'key') and n1.key == n2.key and n1.key is not None):
                            
                            occurrence = temp_key_counts_list1_for_replace.get(n1_key, 0)
                            temp_key_counts_list1_for_replace[n1_key] = occurrence + 1
                            path_seg = f"{n1_key}___{occurrence}"
                            item_path = current_path_prefix + [path_seg]
                            
                            temp_pairwise_for_replace.append(PairwiseDiffResult('MODIFIED', item_path, n1, n2, l1_idx, l2_idx))
                            if isinstance(n1, PdsBlock) and isinstance(n2, PdsBlock):
                                temp_pairwise_for_replace.extend(self._perform_pairwise_diff(n1.children, n2.children, item_path))
                        else:
                            is_all_modified = False; break 
                    
                    if is_all_modified:
                        pairwise_changes.extend(temp_pairwise_for_replace)
                        key_counts_list1 = temp_key_counts_list1_for_replace # Commit counts
                        continue # Skip default delete/add for this replace block


                # Default 'replace' handling: treat as DELETED from list1 then ADDED from list2
                for k_del in range(i2 - i1): 
                    l1_idx_del = i1 + k_del; node_del = list1_nodes[l1_idx_del]
                    n_del_key = self._get_node_key_for_path(node_del)
                    occ_del = key_counts_list1.get(n_del_key,0); key_counts_list1[n_del_key] = occ_del + 1
                    item_path_del = current_path_prefix + [f"{n_del_key}___{occ_del}"]
                    pairwise_changes.append(PairwiseDiffResult('DELETED', item_path_del, node_del, None, l1_idx_del, -1))
                
                for k_add in range(j2 - j1): 
                    l2_idx_add = j1 + k_add; node_add = list2_nodes[l2_idx_add]
                    n_add_key = self._get_node_key_for_path(node_add)
                    item_path_add = current_path_prefix + [f"{n_add_key}___INSERTED_REPLACE_L1idx_{i1}_L2idx_{l2_idx_add}"]
                    pairwise_changes.append(PairwiseDiffResult('ADDED', item_path_add, None, node_add, -1, l2_idx_add))

            elif tag == 'delete': 
                for k in range(i2 - i1):
                    l1_idx = i1 + k; node_del = list1_nodes[l1_idx]
                    n_del_key = self._get_node_key_for_path(node_del)
                    occ = key_counts_list1.get(n_del_key,0); key_counts_list1[n_del_key] = occ + 1
                    item_path_del = current_path_prefix + [f"{n_del_key}___{occ}"]
                    pairwise_changes.append(PairwiseDiffResult('DELETED', item_path_del, node_del, None, l1_idx, -1))

            elif tag == 'insert': 
                for k in range(j2 - j1):
                    l2_idx = j1 + k; node_add = list2_nodes[l2_idx]
                    n_add_key = self._get_node_key_for_path(node_add)
                    item_path_add = current_path_prefix + [f"{n_add_key}___INSERTED_L1idx_{i1}_L2idx_{l2_idx}"]
                    pairwise_changes.append(PairwiseDiffResult('ADDED', item_path_add, None, node_add, -1, l2_idx))
        return pairwise_changes

    def diff_nodes(self, old_nodes_root: list[PdsNode], mod_nodes_root: list[PdsNode], new_nodes_root: list[PdsNode]):
        # ... (UNCHANGED reconciliation logic) ...
        mod_diffs = self._perform_pairwise_diff(old_nodes_root, mod_nodes_root, [])
        van_diffs = self._perform_pairwise_diff(old_nodes_root, new_nodes_root, [])
        mod_map = {'.'.join(d.path): d for d in mod_diffs}
        van_map = {'.'.join(d.path): d for d in van_diffs}
        all_paths = sorted(list(set(mod_map.keys()) | set(van_map.keys())))
        final_changes = []
        for path_str in all_paths:
            p_list = path_str.split('.'); ctx_path = p_list[:-1]
            mod_c = mod_map.get(path_str); van_c = van_map.get(path_str)
            o,m,n = None,None,None 
            s_om, s_on = 'ABSENT_IN_MOD_DIFF', 'ABSENT_IN_VANILLA_DIFF' 
            if mod_c and mod_c.old_item: o = mod_c.old_item
            elif van_c and van_c.old_item: o = van_c.old_item
            if mod_c: 
                s_om = mod_c.change_type; m = mod_c.new_item 
            elif o : 
                if van_c and van_c.change_type == 'DELETED' and van_c.old_item == o : 
                     s_om = 'DELETED'; m = None
                elif o: s_om = 'IDENTICAL'; m = o 
            if van_c: 
                s_on = van_c.change_type; n = van_c.new_item 
            elif o: 
                if mod_c and mod_c.change_type == 'DELETED' and mod_c.old_item == o:
                    s_on = 'DELETED'; n = None
                elif o: s_on = 'IDENTICAL'; n = o
            if s_om == 'ADDED': o = None 
            if s_on == 'ADDED': o = None 
            if s_om == 'ADDED' and mod_c: m = mod_c.new_item
            if s_on == 'ADDED' and van_c: n = van_c.new_item
            f_type = "UNHANDLED_RECONCILIATION_CASE"
            if s_om == 'IDENTICAL': 
                if s_on == 'IDENTICAL': continue 
                elif s_on == 'MODIFIED': f_type = 'VANILLA_MODIFIED' 
                elif s_on == 'ADDED':    f_type = 'VANILLA_ADDED'    
                elif s_on == 'DELETED':  f_type = 'VANILLA_DELETED'  
            elif s_om == 'MODIFIED': 
                if s_on == 'IDENTICAL':  f_type = 'MOD_MODIFIED' 
                elif s_on == 'MODIFIED': 
                    f_type = 'CONVERGED_MODIFICATION' if self._are_nodes_structurally_equal(m,n) else 'CONFLICT_MODIFIED'
                elif s_on == 'ADDED':    
                    f_type = 'ERROR_MOD_MOD_VAN_ADD_SAME_PATH' 
                elif s_on == 'DELETED':  
                    f_type = 'CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED'
            elif s_om == 'ADDED': 
                if s_on == 'IDENTICAL' or s_on == 'ABSENT_IN_VANILLA_DIFF' or s_on == 'DELETED': 
                    f_type = 'MOD_ADDED' 
                elif s_on == 'MODIFIED': 
                    f_type = 'ERROR_MOD_ADD_VAN_MOD_SAME_PATH'
                elif s_on == 'ADDED':    
                    f_type = 'MOD_ADDED_CONVERGED' if self._are_nodes_structurally_equal(m,n) else 'CONFLICT_ADDITION'
            elif s_om == 'DELETED': 
                if s_on == 'IDENTICAL':  f_type = 'MOD_DELETED' 
                elif s_on == 'MODIFIED': 
                    f_type = 'CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED'
                elif s_on == 'ADDED':    
                    f_type = 'ERROR_MOD_DEL_VAN_ADD_SAME_PATH'
                elif s_on == 'DELETED':  
                    f_type = 'MOD_DELETED_VANILLA_ALSO_DELETED'
            elif s_om == 'ABSENT_IN_MOD_DIFF': 
                if s_on == 'ADDED': f_type = 'VANILLA_ADDED' 
                elif s_on == 'MODIFIED': f_type = 'VANILLA_MODIFIED' 
                elif s_on == 'DELETED': f_type = 'VANILLA_DELETED' 
            if f_type != "UNHANDLED_RECONCILIATION_CASE" and not f_type.startswith("ERROR_"):
                final_o, final_m, final_n = o, m, n
                if f_type == 'VANILLA_ADDED': final_o = None; final_m = None 
                if f_type == 'MOD_ADDED': final_o = None; final_n = None 
                if f_type == 'CONFLICT_ADDITION': final_o = None
                final_changes.append(PdsChange(f_type, p_list, final_o, final_m, final_n, ctx_path))
            elif f_type.startswith("ERROR_") or f_type == "UNHANDLED_RECONCILIATION_CASE":
                 sys.stderr.write(f"DEBUG: Differ unhandled/error: Type='{f_type}' Path: {path_str}\n  OM:'{s_om}' ON:'{s_on}'\n  Old: {o is not None}, Mod: {m is not None}, New: {n is not None}\n")
        return final_changes

    def simulate_three_way_merge(self, changes: list[PdsChange], new_nodes_root_list_original: list[PdsNode]) -> list[PdsNode]:
        global g_subsumed_mod_block_paths_for_simulation
        g_subsumed_mod_block_paths_for_simulation = set() 

        simulated_merged_nodes_root_list = [node.copy() for node in new_nodes_root_list_original]

        print(f"--- PdsDiffer: SIMULATING MERGE ({len(changes)} changes) ---") 
        for change_item in changes:
            _apply_single_change_to_sim_tree(
                simulated_merged_nodes_root_list, 
                change_item, 
                g_subsumed_mod_block_paths_for_simulation
            )
        return simulated_merged_nodes_root_list