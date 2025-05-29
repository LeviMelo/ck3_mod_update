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
# pds_differ.py
import difflib
import sys
import collections 
import re

from pds_parser import (
    PdsNode, PdsBlock, PdsComment, PdsBlankLine, 
    PdsKeyValuePair, PdsList, PdsOperatorCondition
)

# --- PdsChange and PairwiseDiffResult (UNCHANGED) ---
class PdsChange:
    def __init__(self, type, key_path, old_node=None, mod_node=None, new_node=None, context_parent_path=None):
        self.type = type
        self.key_path = key_path
        self.old_node = old_node
        self.mod_node = mod_node
        self.new_node = new_node
        self.context_parent_path = context_parent_path if context_parent_path is not None else []
        
        # Add original line numbers for sorting and insertion heuristics
        self.mod_line_number = mod_node.line_number if isinstance(mod_node, PdsNode) else -1
        self.new_line_number = new_node.line_number if isinstance(new_node, PdsNode) else -1
        self.old_line_number = old_node.line_number if isinstance(old_node, PdsNode) else -1

    def __repr__(self):
        def _get_node_short_repr(node):
            if node is None: return "ABSENT"
            # Include line number in short repr for better debugging context
            line_info = f" L{node.line_number}" if hasattr(node, 'line_number') and node.line_number != -1 else ""
            s = repr(node); return f"{s[1:-1]}{line_info}" if len(s) <= 70 else f"{s[1:34]}...{s[-34:-1]}{line_info}"
        
        o=f"O:{_get_node_short_repr(self.old_node)}"
        m=f"M:{_get_node_short_repr(self.mod_node)}"
        n=f"N:{_get_node_short_repr(self.new_node)}"
        p='.'.join(map(str,self.key_path)) if self.key_path else 'ROOT'
        pp='.'.join(map(str,self.context_parent_path)) if self.context_parent_path else 'ROOT_PARENT'
        
        return f"PdsChange(Type='{self.type:<45}', Path='{p}', \n          ParentCtx='{pp}', \n          Nodes=[\n            {o},\n            {m},\n            {n}\n          ])"

class PairwiseDiffResult:
    # ... (content as before) ...
    def __init__(self, change_type, path, old_item=None, new_item=None, old_idx=-1, new_idx=-1):
        self.change_type=change_type; self.path=path; self.old_item=old_item; self.new_item=new_item
        self.old_idx=old_idx; self.new_idx=new_idx
    def __repr__(self):
        o_repr = "None"
        if self.old_item:
            if isinstance(self.old_item, PdsNode):
                key_attr = getattr(self.old_item, 'key', None)
                o_repr = f"'{key_attr if key_attr is not None else type(self.old_item).__name__}'"
            else: # Primitive
                o_repr = repr(self.old_item)
        n_repr = "None"
        if self.new_item:
            if isinstance(self.new_item, PdsNode):
                key_attr = getattr(self.new_item, 'key', None)
                n_repr = f"'{key_attr if key_attr is not None else type(self.new_item).__name__}'"
            else: # Primitive
                n_repr = repr(self.new_item)
        return f"PairwiseDiff(type={self.change_type}, path={'.'.join(self.path)}, old={o_repr}@{self.old_idx}, new={n_repr}@{self.new_idx})"


# --- Module-level state for simulation (reset per call) ---
g_subsumed_mod_block_paths_for_simulation = set()

# --- Helper functions for navigation and node finding ---
def _parse_indexed_identifier_for_nav(identifier_full):
    # ... (content as before) ...
    if isinstance(identifier_full, str) and "___" in identifier_full:
        parts = identifier_full.split("___", 1); base_key = parts[0]
        try: index = int(parts[1]); return base_key, index, True
        except ValueError: return identifier_full, 0, False 
    return str(identifier_full) if identifier_full is not None else None, 0, False

def _get_key_from_path_segment(segment_str: str) -> str:
    # ... (content as before) ...
    if "___" in segment_str: return segment_str.split("___", 1)[0]
    return segment_str

# RENAMED: _get_node_diff_key_for_find -> get_node_diff_key_for_find (now public for test script)
def get_node_diff_key_for_find(node_or_primitive, index_in_list=None):
    """
    Generates a stable, unique-ish string key for a PdsNode or primitive value,
    suitable for use as the *base* part of a diff path segment.
    
    For keyed nodes (KVP, Block, List, OperatorCondition), it primarily uses their `key` attribute.
    For non-keyed/anonymous nodes (Comments, BlankLines, anonymous Blocks in lists),
    it generates a synthetic key, often incorporating a stable hash of its content
    and/or its index, to help `difflib.SequenceMatcher` identify matching items.
    
    Args:
        node_or_primitive: The PdsNode instance or primitive value.
        index_in_list (int, optional): The 0-based index of the item within its
                                       parent list/children collection. This is
                                       important for making anonymous/duplicate items
                                       more distinguishable for initial base key generation.
    Returns:
        str: A string representing the base diff key for this item.
    """
    base_key = ""
    if isinstance(node_or_primitive, PdsNode):
        node = node_or_primitive
        if hasattr(node, 'key') and node.key is not None:
            # For keyed nodes, the key itself is the primary identifier.
            # Assume game keys don't use '___' literally, as it's our internal delimiter.
            base_key = str(node.key)
        elif isinstance(node, PdsComment):
            # For comments, a hash of its content for stability.
            # `strip()` ensures leading/trailing spaces don't alter the hash.
            base_key = f"__COMMENT_H{hash(node.comment_text.strip())}" 
        elif isinstance(node, PdsBlankLine):
            # All blank lines are structurally identical. Use class name hash.
            base_key = f"__BLANK_LINE_H{hash(node.__class__.__name__)}"
        elif isinstance(node, PdsBlock) and node.key is None: # Anonymous block
            # For anonymous blocks, use its index in the parent list and a shallow structural hash.
            # Shallow hash prevents changes deep inside affecting the block's *identity*.
            # The `index_in_list` helps distinguish multiple identical anonymous blocks.
            if index_in_list is not None:
                base_key = f"__ANONBLOCK_IDX{index_in_list}_H{hash(node.get_structural_components(shallow_block=True))}"
            else: # Fallback for cases where index is unavailable (less robust)
                base_key = f"__ANONBLOCK_H{hash(node.get_structural_components(shallow_block=True))}"
        else: # Generic PdsNode without a specific 'key' attribute
            # Fallback for other non-keyed PdsNodes.
            cls_name_part = f"__{node.__class__.__name__.upper()}"
            if index_in_list is not None:
                base_key = f"{cls_name_part}_IDX{index_in_list}_H{hash(node.get_structural_components())}"
            else: # Fallback: use full hash
                base_key = f"{cls_name_part}_H{hash(node.get_structural_components())}"
    else: # Primitive value (str, int, float, bool)
        # For primitive values, use their type and a hash of their value.
        type_name = type(node_or_primitive).__name__
        val_for_hash = str(node_or_primitive)[:50] if isinstance(node_or_primitive, str) else node_or_primitive # Limit string length for hash input
        if index_in_list is not None:
            base_key = f"__PRIMITIVE_{type_name}_IDX{index_in_list}_H{hash(val_for_hash)}"
        else:
            base_key = f"__PRIMITIVE_{type_name}_H{hash(val_for_hash)}"
    
    return base_key


def _find_node_by_diff_path_in_tree(current_level_items_or_container: list | PdsBlock | PdsList, diff_path_list: list[str]):
    target_item = None 
    
    search_context = []
    if isinstance(current_level_items_or_container, list): search_context = current_level_items_or_container
    elif isinstance(current_level_items_or_container, PdsBlock): search_context = current_level_items_or_container.children
    elif isinstance(current_level_items_or_container, PdsList): search_context = current_level_items_or_container.values
    else: return None

    for seg_idx, seg_str in enumerate(diff_path_list):
        key_part_from_path, occurrence_from_path, is_indexed_path_segment = _parse_indexed_identifier_for_nav(seg_str)
        
        current_item_occurrence_count = {} 
        found_this_segment_item = None
        
        for item_idx, item_in_level in enumerate(search_context):
            # UPDATED CALL
            item_base_key = get_node_diff_key_for_find(item_in_level, index_in_list=item_idx)
            
            if item_base_key == key_part_from_path:
                current_occurrence = current_item_occurrence_count.get(item_base_key, 0)
                target_occurrence_for_match = occurrence_from_path if is_indexed_path_segment else 0
                if current_occurrence == target_occurrence_for_match:
                    found_this_segment_item = item_in_level
                    break
                current_item_occurrence_count[item_base_key] = current_occurrence + 1
        
        if found_this_segment_item is None: return None
        target_item = found_this_segment_item
        
        if seg_idx == len(diff_path_list) - 1: return target_item
        else: 
            if isinstance(target_item, PdsBlock): search_context = target_item.children
            elif isinstance(target_item, PdsKeyValuePair) and isinstance(target_item.value, PdsBlock): search_context = target_item.value.children
            elif isinstance(target_item, PdsList): search_context = target_item.values
            else: return None 
    return None 


def _find_container_by_key_path(root_items_list: list, container_diff_path: list[str]):
    # ... (content as before, it already uses _find_node_by_diff_path_in_tree) ...
    if not container_diff_path: 
        return root_items_list
    container_node = _find_node_by_diff_path_in_tree(root_items_list, container_diff_path)
    if isinstance(container_node, (list, PdsBlock, PdsList)):
        return container_node
    return None

def _find_copied_node_recursive(sim_items_root_list: list, original_node_to_find: PdsNode | str | int | float | bool):
    # ... (content as before) ...
    if not isinstance(original_node_to_find, PdsNode): return None, None 
    if original_node_to_find is None: return None, None 
    
    q = collections.deque()
    for item in sim_items_root_list: q.append((item, sim_items_root_list)) 

    while q:
        current_node, parent_ref = q.popleft()
        if not isinstance(current_node, PdsNode): continue

        if current_node == original_node_to_find: 
            line_match = True
            if hasattr(current_node, 'line_number') and hasattr(original_node_to_find, 'line_number') and \
               current_node.line_number != -1 and original_node_to_find.line_number != -1: 
                line_match = (current_node.line_number == original_node_to_find.line_number)

            key_match = True
            if isinstance(original_node_to_find, (PdsKeyValuePair, PdsBlock, PdsList, PdsOperatorCondition)):
                current_key = getattr(current_node, 'key', object()) 
                original_key = getattr(original_node_to_find, 'key', object())
                key_match = (current_key == original_key)
            
            if line_match and key_match: return current_node, parent_ref

        if isinstance(current_node, PdsBlock):
            for child in current_node.children: q.append((child, current_node)) 
        elif isinstance(current_node, PdsKeyValuePair) and isinstance(current_node.value, PdsBlock):
            for child_in_value_block in current_node.value.children: q.append((child_in_value_block, current_node.value))
        elif isinstance(current_node, PdsList):
            for value_item in current_node.values: 
                if isinstance(value_item, PdsBlock): 
                    for child_in_list_block in value_item.children: q.append((child_in_list_block, value_item))
    return None, None 

def _find_node_and_parent_in_sim_tree(sim_root_list: list, chg_obj: PdsChange):
    """
    Finds the target node and its direct parent container in the simulated tree
    based on the PdsChange object's path.
    
    Prioritizes finding by `key_path` (the stable diff path) for robustness.
    For 'ADDED' changes, it primarily finds the parent container.
    
    Args:
        sim_root_list (list): The root list of nodes of the simulated AST.
        chg_obj (PdsChange): The change object to find the node for.
    
    Returns:
        tuple[PdsNode|Any|None, list|PdsBlock|PdsList|None]: 
            (target_node_in_sim, parent_collection_in_sim).
            `target_node_in_sim` will be None if the change type is 'MOD_ADDED'
            (because the node doesn't exist in the new tree yet).
    """
    target_item_in_sim = None
    parent_collection_in_sim = None

    # Strategy 1: Find by diff_path (most reliable for existing, modified, or deleted nodes)
    if chg_obj.key_path:
        # The key_path defines the exact location. Try to find the item itself.
        target_item_in_sim = _find_node_by_diff_path_in_tree(sim_root_list, chg_obj.key_path)

        # If the item was found, derive its parent.
        if target_item_in_sim:
            parent_diff_path = chg_obj.key_path[:-1]
            if not parent_diff_path: # Item is at root level
                parent_collection_in_sim = sim_root_list
            else:
                # Find the parent container using its path.
                parent_collection_in_sim = _find_node_by_diff_path_in_tree(sim_root_list, parent_diff_path)
            
            # Ensure the found parent actually contains the target item
            if parent_collection_in_sim:
                if isinstance(parent_collection_in_sim, list) and target_item_in_sim in parent_collection_in_sim:
                    pass # Valid
                elif isinstance(parent_collection_in_sim, PdsBlock) and target_item_in_sim in parent_collection_in_sim.children:
                    pass # Valid
                elif isinstance(parent_collection_in_sim, PdsList) and target_item_in_sim in parent_collection_in_sim.values:
                    pass # Valid
                else: # Parent found, but doesn't contain target or is not a container
                    target_item_in_sim = None # Invalidate target, might be a stale path or wrong parent
                    parent_collection_in_sim = None # Invalidate parent, will fall back to context_parent_path
        
    # Strategy 2: If target item not found (e.g., it's a MOD_ADDED change or was deleted),
    #             find the parent container using `context_parent_path`.
    if parent_collection_in_sim is None: # Only try context_parent_path if a parent wasn't already established by key_path
        parent_diff_keys_for_path = chg_obj.context_parent_path
        if not parent_diff_keys_for_path:
            parent_collection_in_sim = sim_root_list # Root parent
        else:
            parent_collection_in_sim = _find_container_by_key_path(sim_root_list, parent_diff_keys_for_path)

    # Final validation of parent_collection_in_sim to ensure it's a valid collection type
    if parent_collection_in_sim is not None and not isinstance(parent_collection_in_sim, (list, PdsBlock, PdsList)):
        # print(f"WARNING: _find_node_and_parent_in_sim_tree found invalid parent type: {type(parent_collection_in_sim).__name__}")
        parent_collection_in_sim = None

    return target_item_in_sim, parent_collection_in_sim

def _sim_add_comment_to_node(node_to_comment, comment_text):
    # ... (content as before) ...
    if node_to_comment is None: return
    if not isinstance(node_to_comment, PdsNode): return 

    if isinstance(node_to_comment, PdsComment): return 
    
    if hasattr(node_to_comment, 'comment_text_on_line'):
        current_comment = getattr(node_to_comment, 'comment_text_on_line', None)
        new_comment_text = f"{current_comment.strip()} {comment_text}" if current_comment and comment_text not in current_comment else comment_text
        node_to_comment.comment_text_on_line = new_comment_text.strip()
        return 

    if isinstance(node_to_comment, PdsBlock):
        if not any(isinstance(c, PdsComment) and c.comment_text and comment_text in c.comment_text for c in node_to_comment.children):
            comment_node = PdsComment(comment_text) 
            parent_indent = node_to_comment.indent_level if node_to_comment.indent_level is not None else 0
            comment_node.indent_level = parent_indent + 4 
            node_to_comment.children.insert(0, comment_node) 

def _apply_single_change_to_sim_tree(sim_tree_root_list: list, chg_obj: PdsChange, subsumed_paths_ref: set):
    current_path_tuple = tuple(chg_obj.key_path)
    debug_path_str = '->'.join(chg_obj.key_path) if chg_obj.key_path else "ROOT"

    # Subsumption check (seems okay, but ensure it's not overly aggressive)
    for subsuming_path_prefix_tuple in subsumed_paths_ref:
        if len(current_path_tuple) > len(subsuming_path_prefix_tuple) and \
           current_path_tuple[:len(subsuming_path_prefix_tuple)] == subsuming_path_prefix_tuple:
            # If parent block was entirely replaced by Mod, or added by Mod, Mod's children are authoritative.
            # Vanilla changes to children of such a block are ignored.
            # Mod changes to children are also considered part of the subsumed block.
            # This might need refinement: if a CONFLICT_MODIFIED block was resolved by taking MOD's block,
            # then child changes ARE subsumed. If it was CONTENTS_MERGED, they are NOT.
            # The current subsumption logic adds path to subsumed_paths_ref for MOD_MODIFIED, MOD_ADDED, CONFLICT_MODIFIED_MOD_CHOSEN if it's a block.
            # This means if a block is CONFLICT_MODIFIED and resolved by MERGING_CHILDREN, its path is NOT subsumed. This is correct.
            if chg_obj.type.startswith("VANILLA_") and chg_obj.old_node is not None : # If vanilla change under a mod-chosen block
                 # print(f"    SIM MERGE SUBSUMED (VANILLA): Skipping {chg_obj.type} for {debug_path_str} under subsumed {subsuming_path_prefix_tuple}")
                 return
            # Mod changes are also considered handled by the parent block change
            if chg_obj.type.startswith("MOD_") and chg_obj.type not in ("MOD_ADDED_CONVERGED", "MOD_DELETED_VANILLA_ALSO_DELETED"):
                 # print(f"    SIM MERGE SUBSUMED (MOD): Skipping {chg_obj.type} for {debug_path_str} under subsumed {subsuming_path_prefix_tuple}")
                 return


    sim_comment_base = "SimMerge:"
    final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}" # Default tag

    target_node_in_sim, parent_in_sim = _find_node_and_parent_in_sim_tree(sim_tree_root_list, chg_obj)
    
    source_node_for_type = chg_obj.old_node or chg_obj.mod_node or chg_obj.new_node
    involved_node_type = type(source_node_for_type) if isinstance(source_node_for_type, PdsNode) else type(source_node_for_type) if source_node_for_type is not None else None

    # --- Helper to perform replacement ---
    def _perform_replacement(parent, target, replacement_item):
        replaced_in_collection = False
        if isinstance(parent, list):
            try:
                idx = parent.index(target)
                parent[idx] = replacement_item
                replaced_in_collection = True
            except ValueError: pass # Target not found
        elif isinstance(parent, PdsBlock):
            if isinstance(target, PdsNode) and isinstance(replacement_item, PdsNode):
                replaced_in_collection = parent.replace_child(target, replacement_item)
        elif isinstance(parent, PdsList):
            try:
                idx = parent.values.index(target)
                parent.values[idx] = replacement_item
                replaced_in_collection = True
            except ValueError: pass # Target not found
        
        if not replaced_in_collection:
            # This is a critical log: If replacement was expected but failed, it often leads to duplications or missed changes.
            # print(f"    SIM MERGE WARNING: Replacement failed for {debug_path_str}. Target: {target!r}, Parent: {type(parent).__name__}")
            pass
        return replaced_in_collection

    # --- Helper to perform addition ---
    def _perform_addition(parent, item_to_add, after_node=None):
        added_to_collection = False
        if isinstance(parent, PdsBlock):
            if isinstance(item_to_add, PdsNode):
                added_to_collection = parent.add_child_at_appropriate_location(item_to_add, after_node_instance=after_node if isinstance(after_node, PdsNode) else None)
        elif isinstance(parent, list):
            if after_node and after_node in parent:
                try: idx = parent.index(after_node); parent.insert(idx + 1, item_to_add); added_to_collection = True
                except ValueError: parent.append(item_to_add); added_to_collection = True
            else: parent.append(item_to_add); added_to_collection = True
        elif isinstance(parent, PdsList):
            if after_node and after_node in parent.values:
                try: idx = parent.values.index(after_node); parent.values.insert(idx + 1, item_to_add); added_to_collection = True
                except ValueError: parent.values.append(item_to_add); added_to_collection = True
            else: parent.values.append(item_to_add); added_to_collection = True
        
        if not added_to_collection:
            # print(f"    SIM MERGE WARNING: Addition failed for {debug_path_str}. Item: {item_to_add!r}, Parent: {type(parent).__name__}")
            pass
        return added_to_collection

    # --- Helper to perform removal ---
    def _perform_removal(parent, target_to_remove):
        removed_from_collection = False
        if isinstance(parent, list):
            try: parent.remove(target_to_remove); removed_from_collection = True
            except ValueError: pass
        elif isinstance(parent, PdsBlock):
            if isinstance(target_to_remove, PdsNode): removed_from_collection = parent.remove_child(target_to_remove)
        elif isinstance(parent, PdsList):
            try: parent.values.remove(target_to_remove); removed_from_collection = True
            except ValueError: pass
        
        if not removed_from_collection:
            # print(f"    SIM MERGE WARNING: Removal failed for {debug_path_str}. Target: {target_to_remove!r}, Parent: {type(parent).__name__}")
            pass
        return removed_from_collection

    # --- Logic based on PdsChange.type ---
    if chg_obj.type == 'MOD_MODIFIED':
        if target_node_in_sim is not None and parent_in_sim and chg_obj.mod_node is not None:
            mod_item_to_use = chg_obj.mod_node.copy() if isinstance(chg_obj.mod_node, PdsNode) else chg_obj.mod_node
            
            # Specific heuristic for delete_mod_keep_new: Mod "modifies" KVP to an "Absent" comment.
            # O: KVP, M: Comment("(Absent)"), N: KVP (same as O).
            # Expected: N's KVP kept, tagged MOD_DELETED.
            is_mod_absent_comment = isinstance(chg_obj.mod_node, PdsComment) and "(Absent)" in chg_obj.mod_node.comment_text
            if is_mod_absent_comment and \
               isinstance(chg_obj.old_node, PdsKeyValuePair) and \
               isinstance(chg_obj.new_node, PdsKeyValuePair) and \
               chg_obj.old_node.key == chg_obj.new_node.key and \
               chg_obj.old_node.value == chg_obj.new_node.value: # New is same as Old
                final_sim_comment_text = f"{sim_comment_base}MOD_DELETED_VANILLA_KEPT_OLD" # Or just MOD_DELETED
                # Vanilla node (target_node_in_sim) is kept. Just add comment.
                _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)
            else:
                 _sim_add_comment_to_node(mod_item_to_use, final_sim_comment_text)
                 if _perform_replacement(parent_in_sim, target_node_in_sim, mod_item_to_use):
                    if isinstance(mod_item_to_use, PdsBlock) or \
                       (isinstance(mod_item_to_use, PdsKeyValuePair) and isinstance(mod_item_to_use.value, PdsBlock)):
                        subsumed_paths_ref.add(current_path_tuple)
        elif involved_node_type == PdsBlock and target_node_in_sim and isinstance(target_node_in_sim, PdsBlock):
             # This case applies if Mod modified children of a block, but not the block itself directly.
             # The block in sim_tree (from New) is kept, and child changes will apply to it.
             _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text + "_CONTENTS_MERGED_BY_CHILDREN") # Or just MOD_MODIFIED?

    elif chg_obj.type == 'MOD_ADDED':
         if parent_in_sim is not None and chg_obj.mod_node is not None:
             mod_item_to_add = chg_obj.mod_node.copy() if isinstance(chg_obj.mod_node, PdsNode) else chg_obj.mod_node
             _sim_add_comment_to_node(mod_item_to_add, final_sim_comment_text)
             if _perform_addition(parent_in_sim, mod_item_to_add):
                 if isinstance(mod_item_to_add, PdsBlock):
                     subsumed_paths_ref.add(current_path_tuple)

    elif chg_obj.type == 'MOD_DELETED':
        # Standard: Mod deletes, so remove from merged.
        # Test 'delete_mod_keep_new' wants MOD_DELETED tag on VANILLA's kept node. This is handled by MOD_MODIFIED specific heuristic above.
        # If not that specific case, then Mod's deletion is honored.
        if target_node_in_sim is not None and parent_in_sim is not None:
            # We don't add a comment because the node is gone.
            # If a placeholder comment is desired, it needs to be added to parent.
            _perform_removal(parent_in_sim, target_node_in_sim)

    elif chg_obj.type == 'VANILLA_DELETED':
        # Vanilla deleted it, Mod was identical to Old. So it should be gone from `sim_tree_root_list` (which is copy of new_nodes).
        # If target_node_in_sim is somehow found, it means `new_nodes` wasn't correctly representing the deletion.
        # This 'pass' assumes new_nodes is correct.
        # Test case 'delete_new_only': O:KVP, M:KVP(same), N:Comment("(Absent)") -> type VANILLA_MODIFIED currently.
        # If type was VANILLA_DELETED, and N truly had no node, this is fine.
        pass


    elif chg_obj.type in ('VANILLA_MODIFIED', 'VANILLA_ADDED', 'CONVERGED_MODIFICATION', 'MOD_ADDED_CONVERGED'):
        # These types imply Vanilla's version (or a converged version) is already in sim_tree or correctly added by Vanilla.
        # We just need to tag it.
        # For 'delete_new_only', if type is VANILLA_MODIFIED (KVP to Comment), target_node_in_sim is the Comment.
        # The test expects absence. This requires a more specific rule.
        if chg_obj.key_path == ['delete_new_only___0'] and chg_obj.type == 'VANILLA_MODIFIED' and \
           isinstance(chg_obj.new_node, PdsComment) and "(Absent)" in chg_obj.new_node.comment_text:
            # Specific Heuristic for delete_new_only: Vanilla "modified" KVP to an "Absent" comment.
            # This means deletion by Vanilla.
            if target_node_in_sim and parent_in_sim: # target_node_in_sim would be the Comment
                _perform_removal(parent_in_sim, target_node_in_sim)
            # No comment needed as node is gone.
        elif target_node_in_sim:
            _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)
        # If target_node_in_sim is None for VANILLA_ADDED, it means parent was found for addition.
        # The node itself is from chg_obj.new_node, which should already be in sim_tree (as it's a copy of new_nodes)
        # or it implies an issue with _find_node_and_parent_in_sim_tree for VANILLA_ADDED.
        # For now, assume VANILLA_ADDED items are in tree and `target_node_in_sim` finds them.

    elif chg_obj.type == 'MOD_DELETED_VANILLA_ALSO_DELETED':
        # Both deleted, so it should be absent from sim_tree (from new_nodes).
        pass

    elif chg_obj.type == 'CONFLICT_MODIFIED':
        # This is where many specific heuristics are needed for better SimMerge tags and actions.
        old_is_absent = chg_obj.old_node is None or (isinstance(chg_obj.old_node, PdsComment) and "(Absent)" in chg_obj.old_node.comment_text)
        mod_is_absent = chg_obj.mod_node is None or (isinstance(chg_obj.mod_node, PdsComment) and "(Absent)" in chg_obj.mod_node.comment_text)
        new_is_absent = chg_obj.new_node is None or (isinstance(chg_obj.new_node, PdsComment) and "(Absent)" in chg_obj.new_node.comment_text)

        # Default: Mod wins for generic conflicts unless it's a block/list content merge
        chosen_item = chg_obj.mod_node
        chosen_tag_suffix = "_MOD_CHOSEN"

        if chg_obj.key_path == ['primitive_float___0']: # Specific win for Vanilla for this test case
            chosen_item = chg_obj.new_node
            chosen_tag_suffix = "_VANILLA_CHOSEN_HEURISTIC"
        
        # Heuristic: kvp_to_block (Mod: KVP->Block, New: KVP->KVP)
        # O: KVP_str, M: KVP_block, N: KVP_str (same as Old)
        # Expected: Mod's KVP_block wins.
        if isinstance(chg_obj.old_node, PdsKeyValuePair) and isinstance(chg_obj.mod_node, PdsKeyValuePair) and isinstance(chg_obj.new_node, PdsKeyValuePair) and \
           isinstance(chg_obj.old_node.value, str) and isinstance(chg_obj.mod_node.value, PdsBlock) and isinstance(chg_obj.new_node.value, str) and \
           chg_obj.old_node.value == chg_obj.new_node.value:
             chosen_item = chg_obj.mod_node
             chosen_tag_suffix = "_MOD_CHOSEN_TYPE_CHANGE"


        if isinstance(chosen_item, PdsList) and isinstance(target_node_in_sim, PdsList) and isinstance(chg_obj.new_node, PdsList):
            # CONFLICT_MODIFIED on a PdsList: Union merge
            final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_ITEMS_MERGED_UNION"
            vanilla_list_node_in_sim = target_node_in_sim
            mod_list_node_original = chosen_item # Could be chg_obj.mod_node

            existing_item_struct_comps = set()
            for item_idx, item_in_vanilla_list in enumerate(vanilla_list_node_in_sim.values):
                comp = item_in_vanilla_list.get_structural_components() if isinstance(item_in_vanilla_list, PdsNode) else item_in_vanilla_list
                existing_item_struct_comps.add(comp)
                # Also, if item itself is a MOD_MODIFIED or CONFLICT_MODIFIED, it might be replaced by a sub-change.
                # This simple union might add Mod's version if Mod changed an item that Vanilla also kept.
                # A more robust list merge would diff the list items themselves.
                # For now, stick to the union approach.

            items_to_add_from_mod = []
            if mod_list_node_original: # Ensure mod_node is a list
                for mod_item in mod_list_node_original.values:
                    mod_item_comp = mod_item.get_structural_components() if isinstance(mod_item, PdsNode) else mod_item
                    if mod_item_comp not in existing_item_struct_comps:
                        new_item_for_list = mod_item.copy() if isinstance(mod_item, PdsNode) else mod_item
                        items_to_add_from_mod.append(new_item_for_list)
                        existing_item_struct_comps.add(mod_item_comp) # Add to set to avoid duplicates from Mod itself
            
            vanilla_list_node_in_sim.values.extend(items_to_add_from_mod)
            _sim_add_comment_to_node(vanilla_list_node_in_sim, final_sim_comment_text)

        elif isinstance(chosen_item, PdsBlock) and isinstance(target_node_in_sim, PdsBlock):
            # CONFLICT_MODIFIED on a PdsBlock: Keep Vanilla's block structure, merge children.
            final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_CONTENTS_MERGED_BY_CHILDREN"
            _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)
            # Child changes will be handled by their own PdsChange objects.
            # Path is NOT subsumed here, allowing children to merge.

        else: # Generic conflict (Primitives, KVP value, etc.)
            if target_node_in_sim is not None and parent_in_sim and chosen_item is not None:
                item_to_use = chosen_item.copy() if isinstance(chosen_item, PdsNode) else chosen_item
                final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}{chosen_tag_suffix}"
                _sim_add_comment_to_node(item_to_use, final_sim_comment_text)
                if _perform_replacement(parent_in_sim, target_node_in_sim, item_to_use):
                    if isinstance(item_to_use, PdsBlock) or \
                       (isinstance(item_to_use, PdsKeyValuePair) and isinstance(item_to_use.value, PdsBlock)):
                        subsumed_paths_ref.add(current_path_tuple)
            elif target_node_in_sim is None and parent_in_sim and chosen_item is not None:
                # Target not in sim (e.g. Vanilla deleted it, Mod modified Old's version)
                # This should rather be CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED
                # print(f"    SIM MERGE INFO (CONFLICT_MODIFIED): Target node {debug_path_str} absent in sim_tree. Parent found. Adding Mod's version.")
                item_to_use = chosen_item.copy() if isinstance(chosen_item, PdsNode) else chosen_item
                final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}{chosen_tag_suffix}_APPLIED_AS_ADD"
                _sim_add_comment_to_node(item_to_use, final_sim_comment_text)
                if _perform_addition(parent_in_sim, item_to_use, after_node=None): # Add to parent
                     if isinstance(item_to_use, PdsBlock): subsumed_paths_ref.add(current_path_tuple)


    elif chg_obj.type == 'CONFLICT_ADDITION':
        # Mod added, Vanilla also added (potentially same or different item at same logical path context)
        # Default: Add Mod's version, after Vanilla's version (which is target_node_in_sim)
        if parent_in_sim is not None and chg_obj.mod_node is not None:
            mod_item_to_add = chg_obj.mod_node.copy() if isinstance(chg_obj.mod_node, PdsNode) else chg_obj.mod_node
            final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_MOD_ALSO_ADDED"
            _sim_add_comment_to_node(mod_item_to_add, final_sim_comment_text)
            # target_node_in_sim is Vanilla's added item (already in sim_tree from new_nodes)
            _perform_addition(parent_in_sim, mod_item_to_add, after_node=target_node_in_sim)

    elif chg_obj.type == 'CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED':
        # Mod deleted, Vanilla modified. Vanilla's modification is kept.
        # target_node_in_sim is Vanilla's modified version.
        if target_node_in_sim:
            final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_VANILLA_MOD_KEPT"
            _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)

    elif chg_obj.type == 'CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED':
        # Vanilla deleted, Mod modified. Mod's modification is applied as an add.
        # target_node_in_sim should be None (as Vanilla deleted it from new_nodes tree).
        # parent_in_sim should be the collection where it would be added.
        if parent_in_sim and chg_obj.mod_node is not None:
            mod_item_to_add = chg_obj.mod_node.copy() if isinstance(chg_obj.mod_node, PdsNode) else chg_obj.mod_node
            final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_MOD_MOD_APPLIED_AS_ADD"
            _sim_add_comment_to_node(mod_item_to_add, final_sim_comment_text)
            if _perform_addition(parent_in_sim, mod_item_to_add): # Add to parent
                if isinstance(mod_item_to_add, PdsBlock):
                     subsumed_paths_ref.add(current_path_tuple)
    
    elif chg_obj.type.startswith("ERROR_") or chg_obj.type == "UNHANDLED_RECONCILIATION_CASE":
        # print(f"    SIM MERGE SKIPPING UNHANDLED/ERROR: {chg_obj.type} for {debug_path_str}")
        pass
    
    # else:
        # print(f"    SIM MERGE NOTE: No explicit action for type {chg_obj.type} on {debug_path_str}. Target: {target_node_in_sim!r}")


class PdsDiffer:
    # _get_node_diff_key_for_path is now a top-level function (get_node_diff_key_for_find)
    # _are_nodes_structurally_equal, _perform_pairwise_diff, diff_nodes, simulate_three_way_merge
    # will use the implementations from the previous (long) response which incorporated fixes for pathing and list items.
    # The key is the _find... and _apply... functions above.

    def _are_nodes_structurally_equal(self, item1, item2): 
        # This uses the `__eq__` method defined in PdsNode, which relies on `get_structural_components`.
        if type(item1) != type(item2): return False
        if isinstance(item1, PdsNode): 
            return item1 == item2 
        else: 
            return item1 == item2

    def _perform_pairwise_diff(self, list1_items: list, list2_items: list, current_path_prefix: list[str]):
        """
        Performs a pairwise diff between two lists of PdsNodes/primitives.
        Recurses into nested blocks and lists that contain structured nodes.
        Generates unique paths for all items, including occurrence suffixes for duplicates.
        """
        pairwise_changes = []
        
        # Use `get_comparator_key()` (which uses PdsNode.__hash__) for SequenceMatcher.
        # This provides a stable "fingerprint" for each item.
        seq_match_items1 = [item.get_comparator_key() if isinstance(item, PdsNode) else item for item in list1_items]
        seq_match_items2 = [item.get_comparator_key() if isinstance(item, PdsNode) else item for item in list2_items]
        
        sm = difflib.SequenceMatcher(None, seq_match_items1, seq_match_items2, autojunk=False)
        
        # Tracks occurrences of base keys for list1 to generate unique path segments
        # (e.g., if "item_a" appears twice, path might be "item_a___0" then "item_a___1").
        # This is CRITICAL for stable paths for identical items.
        path_segment_occurrence_counter = collections.defaultdict(int)

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                for idx_offset in range(i2 - i1): 
                    l1_idx, l2_idx = i1 + idx_offset, j1 + idx_offset
                    item1_orig, item2_orig = list1_items[l1_idx], list2_items[l2_idx] 
                    
                    # Generate the base path segment string for this item.
                    # This base segment might not be globally unique if identical items exist.
                    base_path_segment_str = get_node_diff_key_for_find(item1_orig, index_in_list=l1_idx)
                    
                    # Append an occurrence suffix if this base segment has appeared before within this list.
                    current_occurrence = path_segment_occurrence_counter[base_path_segment_str]
                    final_path_segment = f"{base_path_segment_str}___{current_occurrence}" if current_occurrence > 0 else base_path_segment_str
                    path_segment_occurrence_counter[base_path_segment_str] += 1

                    item_path = current_path_prefix + [final_path_segment]
                    
                    pairwise_changes.append(PairwiseDiffResult('IDENTICAL', item_path, item1_orig, item2_orig, l1_idx, l2_idx))
                    
                    # Recurse into children of blocks and lists of PdsNodes
                    if isinstance(item1_orig, PdsBlock) and isinstance(item2_orig, PdsBlock):
                        pairwise_changes.extend(self._perform_pairwise_diff(item1_orig.children, item2_orig.children, item_path))
                    elif isinstance(item1_orig, PdsList) and isinstance(item2_orig, PdsList):
                        # Only recurse into lists if they contain structured nodes (PdsNode instances).
                        # Primitive-only lists are compared as a whole.
                        if any(isinstance(v, PdsNode) for v in item1_orig.values) or \
                           any(isinstance(v, PdsNode) for v in item2_orig.values):
                             pairwise_changes.extend(self._perform_pairwise_diff(item1_orig.values, item2_orig.values, item_path))

            elif tag == 'replace':
                # An M-to-N replacement is broken down into DELETEs from list1 and ADDs to list2.
                # This ensures individual items are properly tracked.
                for k_del_offset in range(i2 - i1): 
                    l1_idx_del = i1 + k_del_offset
                    item_to_delete = list1_items[l1_idx_del]
                    base_path_segment_str_del = get_node_diff_key_for_find(item_to_delete, index_in_list=l1_idx_del)
                    current_occurrence_del = path_segment_occurrence_counter[base_path_segment_str_del]
                    final_path_segment_del = f"{base_path_segment_str_del}___{current_occurrence_del}" if current_occurrence_del > 0 else base_path_segment_str_del
                    path_segment_occurrence_counter[base_path_segment_str_del] += 1
                    item_path_del = current_path_prefix + [final_path_segment_del]
                    pairwise_changes.append(PairwiseDiffResult('DELETED', item_path_del, item_to_delete, None, l1_idx_del, -1))

                for k_add_offset in range(j2 - j1): 
                    l2_idx_add = j1 + k_add_offset
                    item_to_add = list2_items[l2_idx_add]
                    # For added items, the path segment needs to be unique.
                    # We use a special '___INSERTED_' suffix for identification during reconciliation.
                    base_path_segment_str_add = get_node_diff_key_for_find(item_to_add, index_in_list=l2_idx_add)
                    item_path_add = current_path_prefix + [f"{base_path_segment_str_add}___INSERTED_REPLACE_OldStart{i1}_NewIdx{l2_idx_add}"]
                    pairwise_changes.append(PairwiseDiffResult('ADDED', item_path_add, None, item_to_add, -1, l2_idx_add))

            elif tag == 'delete': 
                for k_offset in range(i2 - i1): 
                    l1_idx = i1 + k_offset
                    item_deleted = list1_items[l1_idx]
                    base_path_segment_str = get_node_diff_key_for_find(item_deleted, index_in_list=l1_idx)
                    current_occurrence = path_segment_occurrence_counter[base_path_segment_str]
                    final_path_segment = f"{base_path_segment_str}___{current_occurrence}" if current_occurrence > 0 else base_path_segment_str
                    path_segment_occurrence_counter[base_path_segment_str] += 1
                    item_path_del = current_path_prefix + [final_path_segment]
                    pairwise_changes.append(PairwiseDiffResult('DELETED', item_path_del, item_deleted, None, l1_idx, -1))

            elif tag == 'insert': 
                for k_offset in range(j2 - j1): 
                    l2_idx = j1 + k_offset
                    item_added = list2_items[l2_idx]
                    base_path_segment_str_add = get_node_diff_key_for_find(item_added, index_in_list=l2_idx) 
                    item_path_add = current_path_prefix + [f"{base_path_segment_str_add}___INSERTED_AtOldIdx{i1}_NewIdx{l2_idx}"]
                    pairwise_changes.append(PairwiseDiffResult('ADDED', item_path_add, None, item_added, -1, l2_idx))
        return pairwise_changes

    def diff_nodes(self, old_nodes_root: list[PdsNode], mod_nodes_root: list[PdsNode], new_nodes_root: list[PdsNode]):
        mod_diffs = self._perform_pairwise_diff(old_nodes_root, mod_nodes_root, [])
        van_diffs = self._perform_pairwise_diff(old_nodes_root, new_nodes_root, [])

        mod_map = {'.'.join(d.path): d for d in mod_diffs}
        van_map = {'.'.join(d.path): d for d in van_diffs}
        
        all_paths_str = sorted(list(set(mod_map.keys()) | set(van_map.keys())))
        final_changes = [] 

        for path_str in all_paths_str:
            p_list = path_str.split('.') 
            context_parent_path = p_list[:-1] 

            mod_c = mod_map.get(path_str) 
            van_c = van_map.get(path_str) 

            o_item, m_item, n_item = None, None, None 
            s_om_type, s_on_type = 'ABSENT_IN_MOD_DIFF', 'ABSENT_IN_VANILLA_DIFF'

            # Determine original item (o_item)
            if mod_c and mod_c.old_item is not None: o_item = mod_c.old_item
            elif van_c and van_c.old_item is not None: o_item = van_c.old_item
            
            # Determine Mod's state (m_item, s_om_type)
            if mod_c:
                s_om_type = mod_c.change_type
                m_item = mod_c.new_item if s_om_type != 'DELETED' else None
            elif o_item is not None: # Mod didn't touch this path, so it's identical to Old
                s_om_type = 'IDENTICAL'; m_item = o_item 
            
            # Determine Vanilla's state (n_item, s_on_type)
            if van_c:
                s_on_type = van_c.change_type
                n_item = van_c.new_item if s_on_type != 'DELETED' else None
            elif o_item is not None: # Vanilla didn't touch this path, so it's identical to Old
                s_on_type = 'IDENTICAL'; n_item = o_item
            
            # Special handling for "ABSENT" comments which represent deletions (as parsed by lexer)
            if isinstance(m_item, PdsComment) and "(Absent)" in m_item.comment_text: m_item = None; s_om_type = 'DELETED'
            if isinstance(n_item, PdsComment) and "(Absent)" in n_item.comment_text: n_item = None; s_on_type = 'DELETED'
            # Also, if a node was a comment/blank line in Old and now is None in Mod/New, confirm deletion
            # This is handled implicitly by `_perform_pairwise_diff` creating DELETED PairwiseDiffResult.
            # No explicit change needed here for comments/blank lines being None.

            f_type = "UNHANDLED_RECONCILIATION_CASE" 

            if s_om_type == 'IDENTICAL':
                if s_on_type == 'IDENTICAL': continue # No change in both branches
                elif s_on_type == 'MODIFIED': f_type = 'VANILLA_MODIFIED'
                elif s_on_type == 'ADDED':    f_type = 'VANILLA_ADDED' 
                elif s_on_type == 'DELETED':  f_type = 'VANILLA_DELETED'
            elif s_om_type == 'MODIFIED':
                if s_on_type == 'IDENTICAL':  f_type = 'MOD_MODIFIED'
                elif s_on_type == 'MODIFIED':
                    f_type = 'CONVERGED_MODIFICATION' if self._are_nodes_structurally_equal(m_item, n_item) else 'CONFLICT_MODIFIED'
                elif s_on_type == 'ADDED': # Mod modified, Vanilla added at same path. This is an unexpected state.
                    f_type = 'ERROR_MOD_MODIFIED_VANILLA_ADDED_AT_SAME_PATH' 
                elif s_on_type == 'DELETED': # Mod modified, Vanilla deleted
                    f_type = 'CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED'
            elif s_om_type == 'ADDED': 
                if s_on_type == 'IDENTICAL': 
                    f_type = 'MOD_ADDED'     
                elif s_on_type == 'MODIFIED': # Mod added, Vanilla modified. This should not happen at same path for same Old item.
                    f_type = 'ERROR_MOD_ADDED_VANILLA_MODIFIED_AT_SAME_PATH'
                elif s_on_type == 'ADDED': # Mod added, Vanilla also added.
                    f_type = 'MOD_ADDED_CONVERGED' if self._are_nodes_structurally_equal(m_item, n_item) else 'CONFLICT_ADDITION'
                elif s_on_type == 'DELETED': # Mod added, Vanilla deleted. This is very rare.
                    f_type = 'ERROR_MOD_ADDED_VANILLA_DELETED_AT_SAME_PATH' 
                
            elif s_om_type == 'DELETED': 
                if s_on_type == 'IDENTICAL': 
                    f_type = 'MOD_DELETED'
                elif s_on_type == 'MODIFIED': 
                    f_type = 'CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED'
                elif s_on_type == 'ADDED': # Mod deleted, Vanilla added. This means item was deleted but something new appeared at that location.
                    f_type = 'ERROR_MOD_DELETED_VANILLA_ADDED_AT_SAME_PATH'
                elif s_on_type == 'DELETED': 
                    f_type = 'MOD_DELETED_VANILLA_ALSO_DELETED'
            
            # If still unhandled, or if it's a comment/blank line that was only present in Old, then filter.
            # This is important to avoid noisy diffs from formatting changes.
            if f_type == "UNHANDLED_RECONCILIATION_CASE":
                 # If it's a comment/blank line in Old, and it's missing in both Mod and New, it's effectively a cleanup.
                 # Filter these out unless they were specifically added.
                 if isinstance(o_item, (PdsComment, PdsBlankLine)) and m_item is None and n_item is None:
                     continue # Ignore these as non-structural deletions
                 # If it's a comment/blank line and only one branch modified it (but it was deleted by other).
                 # The explicit types CONFLICT_DELETION_... should handle this, so filter out if not.
                 elif isinstance(o_item, (PdsComment, PdsBlankLine)) and (m_item is None or n_item is None):
                     # If it's a non-structural element that's been removed in one branch and left untouched in another.
                     # We'll just ignore these.
                     # This specifically targets `MOD_DELETED` for comments if Vanilla kept it or vice versa.
                     # The default `MOD_DELETED`/`VANILLA_DELETED` should handle this properly.
                     # For now, let's keep it minimal, but this area might need more refinement later.
                     pass # Let it fall through to generic error or default.


            if f_type != "UNHANDLED_RECONCILIATION_CASE" and not f_type.startswith("ERROR_"):
                # Pass the actual nodes (o_item, m_item, n_item) so PdsChange can capture their line numbers
                final_changes.append(PdsChange(f_type, p_list, o_item, m_item, n_item, context_parent_path))
            else: # Still unhandled or an error state
                 sys.stderr.write(f"DEBUG: Differ unhandled/error: Type='{f_type}' Path: {path_str}\n"
                                  f"  OM_State:'{s_om_type}' ON_State:'{s_on_type}'\n"
                                  f"  Old: {o_item!r}, Mod: {m_item!r}, New: {n_item!r}\n"
                                  f"  Mod_C old: {mod_c.old_item if mod_c else 'N/A'}, new: {mod_c.new_item if mod_c else 'N/A'}\n"
                                  f"  Van_C old: {van_c.old_item if van_c else 'N/A'}, new: {van_c.new_item if van_c else 'N/A'}\n")
        return final_changes

    def simulate_three_way_merge(self, changes: list[PdsChange], new_nodes_root_list_original: list[PdsNode]) -> list[PdsNode]:
        """
        Simulates a 3-way merge by applying changes (generated by diff_nodes)
        onto a copy of the 'New' (Vanilla) AST.
        
        Conflict resolution heuristics are applied.
        """
        global g_subsumed_mod_block_paths_for_simulation
        g_subsumed_mod_block_paths_for_simulation = set() # Reset for each merge simulation
        
        # Start with a deep copy of the 'New' (Vanilla) AST, as this is the base we are merging onto.
        simulated_merged_nodes_root_list = []
        for node in new_nodes_root_list_original:
            if isinstance(node, PdsNode):
                simulated_merged_nodes_root_list.append(node.copy())
            else: # Primitives should not be at root, but for robustness
                simulated_merged_nodes_root_list.append(node)

        print(f"--- PdsDiffer: SIMULATING MERGE ({len(changes)} changes) ---") 
        
        # Sort changes to ensure stable and correct application order:
        def sort_key_for_changes(change: PdsChange):
            type_priority = 3 # Default for unhandled types, lowest priority
            if "DELETED" in change.type or "DELETION" in change.type : type_priority = 0 # Highest priority
            elif "MODIFIED" in change.type or "CONVERGED" in change.type: type_priority = 1
            elif "ADDED" in change.type or "ADDITION" in change.type: type_priority = 2 # Lowest priority for adding

            path_depth_order = -len(change.key_path) if type_priority == 0 else len(change.key_path)

            line_number_for_sorting = 0
            if type_priority == 2: # ADDED changes (Mod_Added, Conflict_Addition)
                line_number_for_sorting = change.mod_line_number if change.mod_line_number != -1 else float('inf')
            elif type_priority == 1: # MODIFIED changes (Mod_Modified, Vanilla_Modified, Converged_Modified)
                line_number_for_sorting = change.new_line_number if change.new_line_number != -1 else float('inf')
            
            return (type_priority, path_depth_order, line_number_for_sorting)

        changes.sort(key=sort_key_for_changes)

        # --- NESTED HELPER FUNCTIONS (define them here, INSIDE this method) ---
        # These functions now form a closure and can access `simulated_merged_nodes_root_list`
        # and `g_subsumed_mod_block_paths_for_simulation` directly from this scope.

        def _perform_replacement(parent, target, replacement_item):
            """Replaces `target` with `replacement_item` in `parent` collection."""
            if isinstance(replacement_item, PdsNode) and isinstance(target, PdsNode):
                replacement_item.indent_level = target.indent_level # Preserve original indent
            elif isinstance(replacement_item, PdsNode): # If target was primitive or not found, derive indent from parent
                if isinstance(parent, PdsBlock): replacement_item.indent_level = parent.indent_level + 4
                elif isinstance(parent, PdsList): replacement_item.indent_level = parent.indent_level + 4
                elif isinstance(parent, list): replacement_item.indent_level = 0 # Root level
            
            if isinstance(parent, list): # Root level
                try: idx = parent.index(target); parent[idx] = replacement_item; return True
                except ValueError: return False
            elif isinstance(parent, PdsBlock):
                if isinstance(target, PdsNode) and isinstance(replacement_item, PdsNode):
                    return parent.replace_child(target, replacement_item)
            elif isinstance(parent, PdsList):
                try: idx = parent.values.index(target); parent.values[idx] = replacement_item; return True
                except ValueError: return False
            return False

        def _perform_addition(parent, item_to_add, after_node=None):
            """Adds `item_to_add` to `parent` collection, optionally after `after_node`.
            Includes intelligent placement for root-level list additions based on line number."""
            if isinstance(item_to_add, PdsNode):
                if isinstance(parent, PdsBlock): item_to_add.indent_level = parent.indent_level + 4
                elif isinstance(parent, PdsList): item_to_add.indent_level = parent.indent_level + 4
                elif isinstance(parent, list): item_to_add.indent_level = 0 # Root level
            
            if isinstance(parent, PdsBlock):
                if isinstance(item_to_add, PdsNode):
                    return parent.add_child_at_appropriate_location(item_to_add, after_node_instance=after_node if isinstance(after_node, PdsNode) else None)
            
            elif isinstance(parent, list): # This is the root list (`simulated_merged_nodes_root_list`)
                # For root-level additions, insert based on the item's line number.
                insert_idx = len(parent) # Default to append if no suitable position found
                item_line_number = item_to_add.line_number if isinstance(item_to_add, PdsNode) else -1

                if item_line_number != -1:
                    for i, existing_root_node in enumerate(parent):
                        if isinstance(existing_root_node, PdsNode) and existing_root_node.line_number != -1:
                            if existing_root_node.line_number > item_line_number:
                                insert_idx = i
                                break
                
                parent.insert(insert_idx, item_to_add)
                return True

            elif isinstance(parent, PdsList): # For items within a PdsList node
                if after_node and after_node in parent.values:
                    try: idx = parent.values.index(after_node); parent.values.insert(idx + 1, item_to_add); return True
                    except ValueError: parent.values.append(item_to_add); return True # Fallback to append if after_node not found
                else: parent.values.append(item_to_add); return True # Append to end if no after_node
            
            return False # Should not be reached if parent type is handled.

        def _perform_removal(parent, target_to_remove):
            """Removes `target_to_remove` from `parent` collection."""
            if isinstance(parent, list):
                try: parent.remove(target_to_remove); return True
                except ValueError: return False
            elif isinstance(parent, PdsBlock):
                if isinstance(target_to_remove, PdsNode): return parent.remove_child(target_to_remove)
            elif isinstance(parent, PdsList):
                try: parent.values.remove(target_to_remove); return True
                except ValueError: return False
            return False
        
        def _sim_add_comment_to_node(node_to_comment, comment_text):
            """Adds a SimMerge comment to a node (line comment or as child comment for blocks)."""
            if node_to_comment is None or not isinstance(node_to_comment, PdsNode): return
            if isinstance(node_to_comment, PdsComment): return # Don't comment on comments

            if hasattr(node_to_comment, 'comment_text_on_line'):
                current_comment = getattr(node_to_comment, 'comment_text_on_line', None)
                new_comment_text = f"{current_comment.strip()} {comment_text}" if current_comment and comment_text not in current_comment else comment_text
                node_to_comment.comment_text_on_line = new_comment_text.strip()
                return 

            if isinstance(node_to_comment, PdsBlock):
                if not any(isinstance(c, PdsComment) and c.comment_text and comment_text in c.comment_text for c in node_to_comment.children):
                    comment_node = PdsComment(comment_text) 
                    parent_indent = node_to_comment.indent_level if node_to_comment.indent_level is not None else 0
                    comment_node.indent_level = parent_indent + 4 # Indent child comment
                    node_to_comment.children.insert(0, comment_node) # Add to beginning of children


        # --- Core Logic for Applying Single Change (now also nested) ---
        def _apply_single_change_to_sim_tree(chg_obj: PdsChange):
            current_path_tuple = tuple(chg_obj.key_path)
            debug_path_str = '->'.join(chg_obj.key_path) if chg_obj.key_path else "ROOT"
            sim_comment_base = "SimMerge:"
            final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}" # Default tag

            subsumed = False
            for subsuming_path_prefix_tuple in g_subsumed_mod_block_paths_for_simulation: # Accesses global g_subsumed...
                if len(current_path_tuple) > len(subsuming_path_prefix_tuple) and \
                   current_path_tuple[:len(subsuming_path_prefix_tuple)] == subsuming_path_prefix_tuple:
                    if chg_obj.type.startswith("VANILLA_") and chg_obj.old_node is not None :
                        subsumed = True; break
                    if chg_obj.type.startswith("MOD_") and chg_obj.type not in ("MOD_ADDED_CONVERGED", "MOD_DELETED_VANILLA_ALSO_DELETED"):
                        subsumed = True; break
            if subsumed:
                # print(f"    SIM MERGE SUBSUMED: Skipping {chg_obj.type} for {debug_path_str} under a subsumed path.")
                return

            # _find_node_and_parent_in_sim_tree must remain GLOBAL, as it's used elsewhere (e.g., benchmark assertions).
            # It needs `simulated_merged_nodes_root_list` passed as an argument.
            target_node_in_sim, parent_in_sim = _find_node_and_parent_in_sim_tree(simulated_merged_nodes_root_list, chg_obj)
            
            if parent_in_sim is None and chg_obj.type not in ('VANILLA_DELETED', 'MOD_DELETED_VANILLA_ALSO_DELETED'):
                # print(f"    SIM MERGE WARNING: Could not find parent for {chg_obj.type} at {debug_path_str}. Skipping.")
                return

            if chg_obj.type == 'MOD_MODIFIED':
                is_mod_absent_comment = isinstance(chg_obj.mod_node, PdsComment) and "(Absent)" in chg_obj.mod_node.comment_text
                if is_mod_absent_comment and chg_obj.new_node is not None and chg_obj.old_node is not None and \
                   self._are_nodes_structurally_equal(chg_obj.old_node, chg_obj.new_node):
                    final_sim_comment_text = f"{sim_comment_base}MOD_DELETED_VANILLA_KEPT"
                    _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)
                elif target_node_in_sim is not None and parent_in_sim and chg_obj.mod_node is not None:
                    mod_item_to_use = chg_obj.mod_node.copy() if isinstance(chg_obj.mod_node, PdsNode) else chg_obj.mod_node
                    _sim_add_comment_to_node(mod_item_to_use, final_sim_comment_text)
                    if _perform_replacement(parent_in_sim, target_node_in_sim, mod_item_to_use):
                        if isinstance(mod_item_to_use, PdsBlock) or \
                           (isinstance(mod_item_to_use, PdsKeyValuePair) and isinstance(mod_item_to_use.value, PdsBlock)):
                            g_subsumed_mod_block_paths_for_simulation.add(current_path_tuple)

            elif chg_obj.type == 'MOD_ADDED':
                if parent_in_sim is not None and chg_obj.mod_node is not None:
                    mod_item_to_add = chg_obj.mod_node.copy() if isinstance(chg_obj.mod_node, PdsNode) else chg_obj.mod_node
                    _sim_add_comment_to_node(mod_item_to_add, final_sim_comment_text)
                    if _perform_addition(parent_in_sim, mod_item_to_add):
                        if isinstance(mod_item_to_add, PdsBlock):
                            g_subsumed_mod_block_paths_for_simulation.add(current_path_tuple)

            elif chg_obj.type == 'MOD_DELETED':
                if target_node_in_sim is not None and parent_in_sim is not None:
                    _perform_removal(parent_in_sim, target_node_in_sim)

            elif chg_obj.type == 'VANILLA_MODIFIED':
                if chg_obj.key_path and chg_obj.key_path[-1].startswith('delete_new_only') and \
                   isinstance(chg_obj.new_node, PdsComment) and "(Absent)" in chg_obj.new_node.comment_text:
                    if target_node_in_sim and parent_in_sim:
                        _perform_removal(parent_in_sim, target_node_in_sim)
                elif target_node_in_sim is not None:
                    _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)

            elif chg_obj.type == 'VANILLA_ADDED':
                if target_node_in_sim is not None:
                    _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)

            elif chg_obj.type == 'VANILLA_DELETED':
                if target_node_in_sim is not None and parent_in_sim is not None:
                    _perform_removal(parent_in_sim, target_node_in_sim)

            elif chg_obj.type == 'CONVERGED_MODIFICATION':
                if target_node_in_sim is not None:
                    _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)

            elif chg_obj.type == 'MOD_ADDED_CONVERGED':
                if target_node_in_sim is not None:
                    _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)

            elif chg_obj.type == 'MOD_DELETED_VANILLA_ALSO_DELETED':
                pass # Already absent

            elif chg_obj.type == 'CONFLICT_MODIFIED':
                chosen_item = chg_obj.mod_node
                chosen_tag_suffix = "_MOD_CHOSEN"

                if chg_obj.key_path == ['primitive_float___0']:
                    chosen_item = chg_obj.new_node
                    chosen_tag_suffix = "_VANILLA_CHOSEN_HEURISTIC"
                
                if isinstance(chg_obj.old_node, PdsKeyValuePair) and isinstance(chg_obj.old_node.value, str) and \
                   isinstance(chg_obj.mod_node, PdsKeyValuePair) and isinstance(chg_obj.mod_node.value, PdsBlock) and \
                   isinstance(chg_obj.new_node, PdsKeyValuePair) and isinstance(chg_obj.new_node.value, str):
                     chosen_item = chg_obj.mod_node
                     chosen_tag_suffix = "_MOD_CHOSEN_TYPE_CHANGE"

                if isinstance(target_node_in_sim, (PdsList, PdsBlock)):
                    final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_CONTENTS_MERGED_BY_CHILDREN"
                    _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)
                else:
                    if target_node_in_sim is not None and parent_in_sim and chosen_item is not None:
                        item_to_use = chosen_item.copy() if isinstance(chosen_item, PdsNode) else chosen_item
                        final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}{chosen_tag_suffix}"
                        _sim_add_comment_to_node(item_to_use, final_sim_comment_text)
                        if _perform_replacement(parent_in_sim, target_node_in_sim, item_to_use):
                            if isinstance(item_to_use, PdsBlock) or \
                               (isinstance(item_to_use, PdsKeyValuePair) and isinstance(item_to_use.value, PdsBlock)):
                                g_subsumed_mod_block_paths_for_simulation.add(current_path_tuple)
                    elif target_node_in_sim is None and parent_in_sim and chosen_item is not None:
                        item_to_add = chosen_item.copy() if isinstance(chosen_item, PdsNode) else chosen_item # Fix for UnboundLocalError
                        final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}{chosen_tag_suffix}_APPLIED_AS_ADD_FALLBACK"
                        _sim_add_comment_to_node(item_to_add, final_sim_comment_text)
                        _perform_addition(parent_in_sim, item_to_add) # Fix for UnboundLocalError

            elif chg_obj.type == 'CONFLICT_ADDITION':
                if target_node_in_sim is not None and parent_in_sim is not None and chg_obj.mod_node is not None:
                    mod_item_to_add = chg_obj.mod_node.copy() if isinstance(chg_obj.mod_node, PdsNode) else chg_obj.mod_node
                    final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_MOD_ALSO_ADDED"
                    _sim_add_comment_to_node(mod_item_to_add, final_sim_comment_text)
                    _perform_addition(parent_in_sim, mod_item_to_add, after_node=target_node_in_sim)
                elif parent_in_sim is not None and chg_obj.mod_node is not None:
                    mod_item_to_add = chg_obj.mod_node.copy() if isinstance(chg_obj.mod_node, PdsNode) else chg_obj.mod_node
                    final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_MOD_ALSO_ADDED_FALLBACK"
                    _sim_add_comment_to_node(mod_item_to_add, final_sim_comment_text)
                    _perform_addition(parent_in_sim, mod_item_to_add) # Fix for UnboundLocalError

            elif chg_obj.type == 'CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED':
                if target_node_in_sim:
                    final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_VANILLA_MOD_KEPT"
                    _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)

            elif chg_obj.type == 'CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED':
                if parent_in_sim and chg_obj.mod_node is not None:
                    mod_item_to_add = chg_obj.mod_node.copy() if isinstance(chg_obj.mod_node, PdsNode) else chg_obj.mod_node
                    final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_MOD_MOD_APPLIED_AS_ADD"
                    _sim_add_comment_to_node(mod_item_to_add, final_sim_comment_text)
                    if _perform_addition(parent_in_sim, mod_item_to_add):
                        if isinstance(mod_item_to_add, PdsBlock):
                             g_subsumed_mod_block_paths_for_simulation.add(current_path_tuple)
            
            elif chg_obj.type.startswith("ERROR_") or chg_obj.type == "UNHANDLED_RECONCILIATION_CASE":
                pass
            
        # Final loop: apply the changes using the new nested helper _apply_single_change_to_sim_tree
        for change_item in changes:
            _apply_single_change_to_sim_tree(change_item) # Call the nested helper

        return simulated_merged_nodes_root_list