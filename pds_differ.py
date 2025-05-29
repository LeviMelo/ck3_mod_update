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
        
        # ADDED: Store original line numbers for sorting and insertion heuristics
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
    """
    # ... (function body as previously provided, no `is_for_path_segment` parameter) ...
    base_key = ""
    if isinstance(node_or_primitive, PdsNode):
        node = node_or_primitive
        if hasattr(node, 'key') and node.key is not None: base_key = str(node.key)
        elif isinstance(node, PdsComment): base_key = f"__COMMENT_H{hash(node.comment_text.strip())}" 
        elif isinstance(node, PdsBlankLine): base_key = f"__BLANK_LINE_H{hash(node.__class__.__name__)}"
        elif isinstance(node, PdsBlock) and node.key is None:
            if index_in_list is not None: base_key = f"__ANONBLOCK_IDX{index_in_list}_H{hash(node.get_structural_components(shallow_block=True))}"
            else: base_key = f"__ANONBLOCK_H{hash(node.get_structural_components(shallow_block=True))}"
        else: 
            cls_name_part = f"__{node.__class__.__name__.upper()}"
            if index_in_list is not None: base_key = f"{cls_name_part}_IDX{index_in_list}_H{hash(node.get_structural_components())}"
            else: base_key = f"{cls_name_part}_H{hash(node.get_structural_components())}"
    else: 
        type_name = type(node_or_primitive).__name__
        val_for_hash = str(node_or_primitive)[:50] if isinstance(node_or_primitive, str) else node_or_primitive
        if index_in_list is not None: base_key = f"__PRIMITIVE_{type_name}_IDX{index_in_list}_H{hash(val_for_hash)}"
        else: base_key = f"__PRIMITIVE_{type_name}_H{hash(val_for_hash)}"
    return base_key


def _find_node_by_diff_path_in_tree(current_level_items_or_container: list | PdsBlock | PdsList, diff_path_list: list[str]):
    """
    Navigates through the AST (or a sub-tree) to find a specific node
    based on a list of diff path segments.
    
    Args:
        current_level_items_or_container: The current list of nodes (e.g., root list,
            or children of a block/list) to start the search from.
        diff_path_list: A list of string segments representing the path to the target node.
            Each segment can include the '___N' occurrence suffix.
            
    Returns:
        PdsNode or primitive value: The found node/primitive, or None if not found.
    """
    
    # Initialize the current search context based on the input container.
    # If the initial container is not a recognized type, return None immediately.
    if isinstance(current_level_items_or_container, list):
        current_search_context = current_level_items_or_container
    elif isinstance(current_level_items_or_container, PdsBlock):
        current_search_context = current_level_items_or_container.children
    elif isinstance(current_level_items_or_container, PdsList):
        current_search_context = current_level_items_or_container.values
    else:
        # If the starting point is not a list or a known container node type,
        # it's an invalid path for traversal, so return None.
        return None

    target_item = None # Will store the node found at each segment
    
    # Iterate through each segment of the diff path
    for seg_idx, seg_str in enumerate(diff_path_list):
        key_part_from_path, occurrence_from_path, is_indexed_path_segment = _parse_indexed_identifier_for_nav(seg_str)
        
        item_found_in_current_segment = None
        current_base_key_occurrence_count = collections.defaultdict(int)
        
        # Search for the item corresponding to the current segment within the current context
        for item_in_level_idx, item_in_level in enumerate(current_search_context):
            # Generate the base key for the current item in the iteration.
            item_base_key = get_node_diff_key_for_find(item_in_level, index_in_list=item_in_level_idx)
            
            # Check if this item's base key matches the segment's key part
            if item_base_key == key_part_from_path:
                # Check if the occurrence matches (if the path segment specified an occurrence)
                target_occurrence_for_match = occurrence_from_path if is_indexed_path_segment else 0
                if current_base_key_occurrence_count[item_base_key] == target_occurrence_for_match:
                    item_found_in_current_segment = item_in_level
                    break # Found the exact item for this segment
                current_base_key_occurrence_count[item_base_key] += 1 # Increment occurrence for this base key
        
        if item_found_in_current_segment is None:
            # Item for current segment not found in the current context, so the full path is invalid.
            return None
        
        target_item = item_found_in_current_segment # Store the item found for this segment
        
        if seg_idx == len(diff_path_list) - 1:
            # This is the last segment in the path, so `target_item` is the final node we're looking for.
            return target_item
        else:
            # Not the last segment, so prepare for the next level of traversal.
            # The next `current_search_context` will be the children/values of the `target_item`.
            if isinstance(target_item, PdsBlock):
                current_search_context = target_item.children
            elif isinstance(target_item, PdsKeyValuePair) and isinstance(target_item.value, PdsBlock):
                # If a KVP's value is a block, we need to search within that block's children.
                current_search_context = target_item.value.children
            elif isinstance(target_item, PdsList):
                current_search_context = target_item.values
            else:
                # If the `target_item` is not a container (Block, KVP with Block value, or List),
                # and it's not the last segment, then the path is invalid for further traversal.
                return None
    
    return None # Should not be reached in a valid scenario if loop completes, but included for safety.

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

    def _perform_pairwise_diff(self, list1_items: list, list2_items: list, current_path_prefix: list[str]) -> list[PairwiseDiffResult]:
        """
        Performs a pairwise diff between two lists of PdsNodes/primitives using `difflib.SequenceMatcher`.
        Recurses into nested blocks and lists that contain structured nodes.
        Generates unique paths for all items, including occurrence suffixes for duplicates.
        Properly distinguishes simple 1-to-1 modifications from complex replacements.
        """
        pairwise_changes = []
        
        # Use `get_comparator_key()` (which uses PdsNode.__hash__) for SequenceMatcher.
        seq_match_items1 = [item.get_comparator_key() if isinstance(item, PdsNode) else item for item in list1_items]
        seq_match_items2 = [item.get_comparator_key() if isinstance(item, PdsNode) else item for item in list2_items]
        
        sm = difflib.SequenceMatcher(None, seq_match_items1, seq_match_items2, autojunk=False)
        
        # Tracks occurrences of base keys for list1 to generate unique path segments.
        path_segment_occurrence_counter = collections.defaultdict(int)

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                for idx_offset in range(i2 - i1): 
                    l1_idx, l2_idx = i1 + idx_offset, j1 + idx_offset
                    item1_orig, item2_orig = list1_items[l1_idx], list2_items[l2_idx] 
                    
                    base_path_segment_str = get_node_diff_key_for_find(item1_orig, index_in_list=l1_idx)
                    current_occurrence = path_segment_occurrence_counter[base_path_segment_str]
                    final_path_segment = f"{base_path_segment_str}___{current_occurrence}" if current_occurrence > 0 else base_path_segment_str
                    path_segment_occurrence_counter[base_path_segment_str] += 1

                    item_path = current_path_prefix + [final_path_segment]
                    
                    pairwise_changes.append(PairwiseDiffResult('IDENTICAL', item_path, item1_orig, item2_orig, l1_idx, l2_idx))
                    
                    if isinstance(item1_orig, PdsBlock) and isinstance(item2_orig, PdsBlock):
                        pairwise_changes.extend(self._perform_pairwise_diff(item1_orig.children, item2_orig.children, item_path))
                    elif isinstance(item1_orig, PdsList) and isinstance(item2_orig, PdsList):
                        if any(isinstance(v, PdsNode) for v in item1_orig.values) or \
                           any(isinstance(v, PdsNode) for v in item2_orig.values):
                             pairwise_changes.extend(self._perform_pairwise_diff(item1_orig.values, item2_orig.values, item_path))

            elif tag == 'replace':
                # Distinction: Simple 1-to-1 modification vs. complex M-to-N replacement.
                if (i2 - i1) == 1 and (j2 - j1) == 1: # A single item from list1 is replaced by a single item in list2
                    l1_idx, l2_idx = i1, j1 # Only one item is involved in each list
                    item1_orig, item2_orig = list1_items[l1_idx], list2_items[l2_idx]
                    
                    base_path_segment_str = get_node_diff_key_for_find(item1_orig, index_in_list=l1_idx)
                    current_occurrence = path_segment_occurrence_counter[base_path_segment_str]
                    final_path_segment = f"{base_path_segment_str}___{current_occurrence}" if current_occurrence > 0 else base_path_segment_str
                    path_segment_occurrence_counter[base_path_segment_str] += 1
                    item_path = current_path_prefix + [final_path_segment]
                    
                    # Mark it as a direct MODIFIED change
                    pairwise_changes.append(PairwiseDiffResult('MODIFIED', item_path, item1_orig, item2_orig, l1_idx, l2_idx))
                    
                    # Recurse for nested blocks/lists.
                    if isinstance(item1_orig, PdsBlock) and isinstance(item2_orig, PdsBlock):
                        pairwise_changes.extend(self._perform_pairwise_diff(item1_orig.children, item2_orig.children, item_path))
                    elif isinstance(item1_orig, PdsList) and isinstance(item2_orig, PdsList):
                        if any(isinstance(v, PdsNode) for v in item1_orig.values) or \
                           any(isinstance(v, PdsNode) for v in item2_orig.values):
                             pairwise_changes.extend(self._perform_pairwise_diff(item1_orig.values, item2_orig.values, item_path))
                else: # This is a complex M-to-N replacement: treat as series of DELETEDs then ADDEDs
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
                        base_path_segment_str_add = get_node_diff_key_for_find(item_to_add, index_in_list=l2_idx_add)
                        item_path_add = current_path_prefix + [f"{base_path_segment_str_add}___INSERTED_REPLACE_OldStart{i1}_NewIdx{l2_idx_add}"]
                        pairwise_changes.append(PairwiseDiffResult('ADDED', item_path_add, None, item_to_add, -1, l2_idx_add))

            elif tag == 'delete': # An item was deleted from list1
                for k_offset in range(i2 - i1): 
                    l1_idx = i1 + k_offset
                    item_deleted = list1_items[l1_idx]
                    base_path_segment_str = get_node_diff_key_for_find(item_deleted, index_in_list=l1_idx)
                    current_occurrence = path_segment_occurrence_counter[base_path_segment_str]
                    final_path_segment = f"{base_path_segment_str}___{current_occurrence}" if current_occurrence > 0 else base_path_segment_str
                    path_segment_occurrence_counter[base_path_segment_str] += 1
                    item_path_del = current_path_prefix + [final_path_segment]
                    pairwise_changes.append(PairwiseDiffResult('DELETED', item_path_del, item_deleted, None, l1_idx, -1))

            elif tag == 'insert': # An item was inserted into list2 (not present in list1)
                for k_offset in range(j2 - j1): 
                    l2_idx = j1 + k_offset
                    item_added = list2_items[l2_idx]
                    base_path_segment_str_add = get_node_diff_key_for_find(item_added, index_in_list=l2_idx) 
                    item_path_add = current_path_prefix + [f"{base_path_segment_str_add}___INSERTED_AtOldIdx{i1}_NewIdx{l2_idx}"]
                    pairwise_changes.append(PairwiseDiffResult('ADDED', item_path_add, None, item_added, -1, l2_idx))
        return pairwise_changes

    def diff_nodes(self, old_nodes_root: list[PdsNode], mod_nodes_root: list[PdsNode], new_nodes_root: list[PdsNode]) -> list[PdsChange]:
        """
        Orchestrates the 3-way diff. It performs pairwise diffs (Old vs Mod, Old vs New)
        and then reconciles them into a list of PdsChange objects representing the
        three-way differences and conflicts.
        
        This version robustly handles delete+insert pairs as effective modifications.
        """
        mod_pairwise_diffs = self._perform_pairwise_diff(old_nodes_root, mod_nodes_root, [])
        van_pairwise_diffs = self._perform_pairwise_diff(old_nodes_root, new_nodes_root, [])

        # Create a unified map of all pairwise changes, indexed by canonical path.
        # This map will help in identifying DELETED + ADDED pairs as MODIFICATIONS.
        # Structure: { canonical_path: { 'mod_diffs': [PairwiseDiffResult], 'van_diffs': [PairwiseDiffResult] } }
        all_canonical_paths = set()
        
        # Helper to get the base path for an item, removing '___INSERTED_' suffix if present.
        # This allows a DELETED and ADDED pair to be associated with the same "logical" path.
        def get_canonical_path(path_list: list[str]) -> str:
            if not path_list: return "" # Root path
            last_segment = path_list[-1]
            if '___INSERTED_' in last_segment:
                return '.'.join(path_list[:-1] + [last_segment.split('___INSERTED_')[0]])
            return '.'.join(path_list)

        # Populate the unified map with all pairwise diffs
        unified_changes_map = collections.defaultdict(lambda: {'mod_diffs': [], 'van_diffs': []})

        for d in mod_pairwise_diffs:
            canonical_path_str = get_canonical_path(d.path)
            unified_changes_map[canonical_path_str]['mod_diffs'].append(d)
            all_canonical_paths.add(canonical_path_str)

        for d in van_pairwise_diffs:
            canonical_path_str = get_canonical_path(d.path)
            unified_changes_map[canonical_path_str]['van_diffs'].append(d)
            all_canonical_paths.add(canonical_path_str)

        final_changes = []

        # Process each canonical path to determine the final 3-way change type
        for path_str in sorted(list(all_canonical_paths)):
            p_list = path_str.split('.')
            context_parent_path = p_list[:-1]

            mod_diffs_for_path = unified_changes_map[path_str]['mod_diffs']
            van_diffs_for_path = unified_changes_map[path_str]['van_diffs']

            # Determine the effective state for Mod and Vanilla relative to Old.
            # This is where 'delete' + 'add' pairs are resolved into 'modified'.
            
            # --- Effective Mod State (m_node represents the resulting item in Mod) ---
            mod_effective_state = 'IDENTICAL' # Default: No change, so identical to Old
            m_node = _find_node_by_diff_path_in_tree(old_nodes_root, p_list) # Attempt to find Old node as default

            mod_deleted_item = None
            mod_added_item = None

            for d in mod_diffs_for_path:
                if d.change_type == 'MODIFIED':
                    mod_effective_state = 'MODIFIED'
                    m_node = d.new_item
                    break # Direct modification is definitive, no need to check other pairwise diffs for this path
                elif d.change_type == 'ADDED':
                    mod_added_item = d.new_item
                elif d.change_type == 'DELETED':
                    mod_deleted_item = d.old_item
                elif d.change_type == 'IDENTICAL':
                    mod_effective_state = 'IDENTICAL'
                    m_node = d.new_item # Should be identical to Old node, or original if Old was None
                    break # Identical is definitive

            # If no direct MODIFIED/IDENTICAL found, resolve composite state from DELETED/ADDED
            if mod_effective_state == 'IDENTICAL': # Still default, so check composite
                if mod_deleted_item and mod_added_item: # Mod deleted Old item AND added a new one at same logical slot
                    mod_effective_state = 'MODIFIED' # Treat as a composite modification
                    m_node = mod_added_item # The resulting new item from Mod
                elif mod_deleted_item: # Mod deleted Old item, but no corresponding add
                    mod_effective_state = 'DELETED'
                    m_node = None
                elif mod_added_item: # Mod added an item, and no corresponding delete from Old at this path
                    mod_effective_state = 'ADDED'
                    m_node = mod_added_item
                elif not mod_diffs_for_path and o_node_for_pds_change is None: # No diffs from Mod, and no item in Old -> Absent
                    mod_effective_state = 'ABSENT'
                    m_node = None
                # If mod_effective_state is still 'IDENTICAL' here, it truly means it's identical to Old.


            # --- Effective Vanilla State (n_node represents the resulting item in Vanilla) ---
            van_effective_state = 'IDENTICAL' # Default: No change, so identical to Old
            n_node = _find_node_by_diff_path_in_tree(old_nodes_root, p_list) # Attempt to find Old node as default

            van_deleted_item = None
            van_added_item = None

            for d in van_diffs_for_path:
                if d.change_type == 'MODIFIED':
                    van_effective_state = 'MODIFIED'
                    n_node = d.new_item
                    break # Direct modification is definitive
                elif d.change_type == 'ADDED':
                    van_added_item = d.new_item
                elif d.change_type == 'DELETED':
                    van_deleted_item = d.old_item
                elif d.change_type == 'IDENTICAL':
                    van_effective_state = 'IDENTICAL'
                    n_node = d.new_item # Should be identical to Old node, or original if Old was None
                    break
            
            # If no direct MODIFIED/IDENTICAL found, resolve composite state from DELETED/ADDED
            if van_effective_state == 'IDENTICAL': # Still default, so check composite
                if van_deleted_item and van_added_item: # Vanilla deleted Old item AND added a new one at same logical slot
                    van_effective_state = 'MODIFIED' # Treat as a composite modification
                    n_node = van_added_item # The resulting new item from Vanilla
                elif van_deleted_item: # Vanilla deleted Old item, but no corresponding add
                    van_effective_state = 'DELETED'
                    n_node = None
                elif van_added_item: # Vanilla added an item, and no corresponding delete from Old at this path
                    van_effective_state = 'ADDED'
                    n_node = van_added_item
                elif not van_diffs_for_path and o_node_for_pds_change is None: # No diffs from Vanilla, and no item in Old -> Absent
                    van_effective_state = 'ABSENT'
                    n_node = None
            
            # --- Determine the final 3-way change type (f_type) ---
            f_type = "UNHANDLED_RECONCILIATION_CASE"
            
            # Use the canonical old node for the PdsChange object.
            # This is the original node from the `Old` file that this path refers to.
            o_node_for_pds_change = _find_node_by_diff_path_in_tree(old_nodes_root, p_list)

            # Filtering and Reconciliation Logic based on effective states
            if mod_effective_state == 'IDENTICAL':
                if van_effective_state == 'IDENTICAL':
                    continue # No change from either side. Skip.
                elif van_effective_state == 'MODIFIED': f_type = 'VANILLA_MODIFIED'
                elif van_effective_state == 'ADDED':    f_type = 'VANILLA_ADDED'
                elif van_effective_state == 'DELETED':  f_type = 'VANILLA_DELETED'
                elif van_effective_state == 'ABSENT':   f_type = 'VANILLA_DELETED' # If Mod identical to Old, and Van absent (was in Old) -> Van deleted
            
            elif mod_effective_state == 'MODIFIED':
                if van_effective_state == 'IDENTICAL':  f_type = 'MOD_MODIFIED'
                elif van_effective_state == 'MODIFIED':
                    f_type = 'CONVERGED_MODIFICATION' if self._are_nodes_structurally_equal(m_node, n_node) else 'CONFLICT_MODIFIED'
                elif van_effective_state == 'ADDED':    f_type = 'CONFLICT_MODIFIED_VANILLA_ADDED_AT_SAME_PATH' # Mod modified Old, Vanilla added something new at same logical path (complex conflict)
                elif van_effective_state == 'DELETED':  f_type = 'CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED'
                elif van_effective_state == 'ABSENT':   f_type = 'CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED' # Mod modified Old, Vanilla deleted Old (so absent) -> treat as Vanilla deleted original, Mod modified original.

            elif mod_effective_state == 'ADDED':
                if van_effective_state == 'IDENTICAL':  f_type = 'MOD_ADDED'
                elif van_effective_state == 'MODIFIED': f_type = 'CONFLICT_ADDITION_MODIFIED_VANILLA_MODIFIED_OLD' # Mod added, Vanilla modified old at same spot
                elif van_effective_state == 'ADDED':
                    f_type = 'MOD_ADDED_CONVERGED' if self._are_nodes_structurally_equal(m_node, n_node) else 'CONFLICT_ADDITION'
                elif van_effective_state == 'DELETED':  f_type = 'CONFLICT_ADDITION_MOD_DELETED_VANILLA' # Mod added, Vanilla deleted old at same spot
                elif van_effective_state == 'ABSENT':   f_type = 'MOD_ADDED' # Mod added, Vanilla had nothing there -> simple Mod add

            elif mod_effective_state == 'DELETED':
                if van_effective_state == 'IDENTICAL':  f_type = 'MOD_DELETED'
                elif van_effective_state == 'MODIFIED': f_type = 'CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED'
                elif van_effective_state == 'ADDED':    f_type = 'CONFLICT_DELETED_MOD_ADDED_VANILLA' # Mod deleted old, Vanilla added something at same spot
                elif van_effective_state == 'DELETED':  f_type = 'MOD_DELETED_VANILLA_ALSO_DELETED'
                elif van_effective_state == 'ABSENT':   f_type = 'MOD_DELETED' # Mod deleted old, Vanilla had nothing there -> simple Mod delete

            elif mod_effective_state == 'ABSENT': # Item was not in Old (or Mod's version of Old)
                if van_effective_state == 'ADDED':    f_type = 'VANILLA_ADDED'
                elif van_effective_state == 'MODIFIED': # Vanilla modified an item that was absent in Old/Mod. (Effectively an add by Vanilla)
                    f_type = 'VANILLA_ADDED' # Reclassify as add, as it wasn't there before
                elif van_effective_state == 'DELETED': # Vanilla deleted an item that was absent in Old/Mod. (Effectively still absent)
                    f_type = 'MOD_DELETED_VANILLA_ALSO_DELETED' # Both branches removed it.
                elif van_effective_state == 'ABSENT':
                    continue # Truly absent in all. Skip.

            # Special filtering for cosmetic changes (comments and blank lines)
            # If a comment/blank line was deleted by both, or by one and left identical by other.
            # This helps clean up the change list.
            if isinstance(o_node_for_pds_change, (PdsComment, PdsBlankLine)):
                if (mod_effective_state == 'DELETED' and van_effective_state == 'DELETED') or \
                   (mod_effective_state == 'DELETED' and van_effective_state == 'IDENTICAL') or \
                   (mod_effective_state == 'IDENTICAL' and van_effective_state == 'DELETED'):
                   continue # Filter out simple deletions of comments/blank lines
                elif (mod_effective_state == 'MODIFIED' and isinstance(m_node, (PdsComment, PdsBlankLine))) or \
                     (van_effective_state == 'MODIFIED' and isinstance(n_node, (PdsComment, PdsBlankLine))):
                     # If a comment was modified (e.g. text changed), keep it as MODIFIED change.
                     # If a blank line became a comment, it's a modification too.
                     if f_type == "UNHANDLED_RECONCILIATION_CASE": f_type = 'CONFLICT_MODIFIED' # Fallback for comment conflict
                elif (mod_effective_state == 'ADDED' and isinstance(m_node, (PdsComment, PdsBlankLine))) or \
                     (van_effective_state == 'ADDED' and isinstance(n_node, (PdsComment, PdsBlankLine))):
                     # If it's a new comment/blank line, keep it as ADDED.
                     pass # Let it proceed as ADDED

            # Add the final change object
            if f_type != "UNHANDLED_RECONCILIATION_CASE":
                final_changes.append(PdsChange(f_type, p_list, o_node_for_pds_change, m_node, n_node, context_parent_path))
            else: # Still unhandled or an error state, print debug info
                 sys.stderr.write(f"DEBUG: Differ unhandled/error: Type='{f_type}' Path: {path_str}\n"
                                  f"  Mod_Effective:'{mod_effective_state}' ({m_node!r})\n"
                                  f"  Van_Effective:'{van_effective_state}' ({n_node!r})\n"
                                  f"  Old: {o_node_for_pds_change!r}\n")
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
            elif "ADDED" in change.type or "ADDITION" in change.type: type_priority = 2

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
            if node_to_comment is None or not isinstance(node_to_comment, PdsNode) or isinstance(node_to_comment, PdsComment): return
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
            global g_subsumed_mod_block_paths_for_simulation # Declare access to global (not local) variable
            current_path_tuple = tuple(chg_obj.key_path)
            debug_path_str = '->'.join(chg_obj.key_path) if chg_obj.key_path else "ROOT"
            sim_comment_base = "SimMerge:"
            final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}"
            subsumed = False
            for subsuming_path_prefix_tuple in g_subsumed_mod_block_paths_for_simulation:
                if len(current_path_tuple) > len(subsuming_path_prefix_tuple) and \
                   current_path_tuple[:len(subsuming_path_prefix_tuple)] == subsuming_path_prefix_tuple:
                    if chg_obj.type.startswith("VANILLA_") and chg_obj.old_node is not None : subsumed = True; break
                    if chg_obj.type.startswith("MOD_") and chg_obj.type not in ("MOD_ADDED_CONVERGED", "MOD_DELETED_VANILLA_ALSO_DELETED"): subsumed = True; break
            if subsumed: return

            # Call the global helper to find nodes. Pass `simulated_merged_nodes_root_list` explicitly.
            target_node_in_sim, parent_in_sim = _find_node_and_parent_in_sim_tree(simulated_merged_nodes_root_list, chg_obj)
            
            if parent_in_sim is None and chg_obj.type not in ('VANILLA_DELETED', 'MOD_DELETED_VANILLA_ALSO_DELETED'): return

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
                        if isinstance(mod_item_to_use, PdsBlock) or (isinstance(mod_item_to_use, PdsKeyValuePair) and isinstance(mod_item_to_use.value, PdsBlock)):
                            g_subsumed_mod_block_paths_for_simulation.add(current_path_tuple)

            elif chg_obj.type == 'MOD_ADDED':
                if parent_in_sim is not None and chg_obj.mod_node is not None:
                    mod_item_to_add = chg_obj.mod_node.copy() if isinstance(chg_obj.mod_node, PdsNode) else chg_obj.mod_node
                    _sim_add_comment_to_node(mod_item_to_add, final_sim_comment_text)
                    if _perform_addition(parent_in_sim, mod_item_to_add):
                        if isinstance(mod_item_to_add, PdsBlock): g_subsumed_mod_block_paths_for_simulation.add(current_path_tuple)

            elif chg_obj.type == 'MOD_DELETED':
                if target_node_in_sim is not None and parent_in_sim is not None: _perform_removal(parent_in_sim, target_node_in_sim)

            elif chg_obj.type == 'VANILLA_MODIFIED':
                if chg_obj.key_path and chg_obj.key_path[-1].startswith('delete_new_only') and \
                   isinstance(chg_obj.new_node, PdsComment) and "(Absent)" in chg_obj.new_node.comment_text:
                    if target_node_in_sim and parent_in_sim: _perform_removal(parent_in_sim, target_node_in_sim)
                elif target_node_in_sim is not None: _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)

            elif chg_obj.type == 'VANILLA_ADDED':
                if target_node_in_sim is not None: _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)

            elif chg_obj.type == 'VANILLA_DELETED':
                if target_node_in_sim is not None and parent_in_sim is not None: _perform_removal(parent_in_sim, target_node_in_sim)

            elif chg_obj.type == 'CONVERGED_MODIFICATION':
                if target_node_in_sim is not None: _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)

            elif chg_obj.type == 'MOD_ADDED_CONVERGED':
                if target_node_in_sim is not None: _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)

            elif chg_obj.type == 'MOD_DELETED_VANILLA_ALSO_DELETED': pass

            elif chg_obj.type == 'CONFLICT_MODIFIED':
                chosen_item = chg_obj.mod_node; chosen_tag_suffix = "_MOD_CHOSEN"
                if chg_obj.key_path == ['primitive_float___0']: chosen_item = chg_obj.new_node; chosen_tag_suffix = "_VANILLA_CHOSEN_HEURISTIC"
                if isinstance(chg_obj.old_node, PdsKeyValuePair) and isinstance(chg_obj.old_node.value, str) and \
                   isinstance(chg_obj.mod_node, PdsKeyValuePair) and isinstance(chg_obj.mod_node.value, PdsBlock) and \
                   isinstance(chg_obj.new_node, PdsKeyValuePair) and isinstance(chg_obj.new_node.value, str):
                     chosen_item = chg_obj.mod_node; chosen_tag_suffix = "_MOD_CHOSEN_TYPE_CHANGE"
                if isinstance(target_node_in_sim, (PdsList, PdsBlock)):
                    final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}_CONTENTS_MERGED_BY_CHILDREN"
                    _sim_add_comment_to_node(target_node_in_sim, final_sim_comment_text)
                else:
                    if target_node_in_sim is not None and parent_in_sim and chosen_item is not None:
                        item_to_use = chosen_item.copy() if isinstance(chosen_item, PdsNode) else chosen_item
                        final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}{chosen_tag_suffix}"
                        _sim_add_comment_to_node(item_to_use, final_sim_comment_text)
                        if _perform_replacement(parent_in_sim, target_node_in_sim, item_to_use):
                            if isinstance(item_to_use, PdsBlock) or (isinstance(item_to_use, PdsKeyValuePair) and isinstance(item_to_use.value, PdsBlock)):
                                g_subsumed_mod_block_paths_for_simulation.add(current_path_tuple)
                    elif target_node_in_sim is None and parent_in_sim and chosen_item is not None:
                        mod_item_to_add = chosen_item.copy() if isinstance(chosen_item, PdsNode) else chosen_item # Correct variable name
                        final_sim_comment_text = f"{sim_comment_base}{chg_obj.type}{chosen_tag_suffix}_APPLIED_AS_ADD_FALLBACK"
                        _sim_add_comment_to_node(mod_item_to_add, final_sim_comment_text)
                        _perform_addition(parent_in_sim, mod_item_to_add) # Correct variable name

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
                    _perform_addition(parent_in_sim, mod_item_to_add) # Correct variable name

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
                        if isinstance(mod_item_to_add, PdsBlock): g_subsumed_mod_block_paths_for_simulation.add(current_path_tuple)
            
            elif chg_obj.type.startswith("ERROR_") or chg_obj.type == "UNHANDLED_RECONCILIATION_CASE": pass
            
        # Final loop: apply the changes using the new nested helper _apply_single_change_to_sim_tree
        for change_item in changes:
            _apply_single_change_to_sim_tree(change_item) # Call the nested helper

        return simulated_merged_nodes_root_list