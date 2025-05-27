import difflib
import sys
#from pds_parser import PdsNode, PdsBlock, PdsComment, PdsBlankLine, PdsKeyValuePair, PdsList, PdsOperatorCondition
# Use a wildcard import for brevity if pds_parser has many node types, or list them all
from pds_parser import PdsNode, PdsBlock, PdsComment, PdsBlankLine, PdsKeyValuePair, PdsList, PdsOperatorCondition


class PdsChange:
    # UNCHANGED from your provided version
    def __init__(self, type, key_path, old_node=None, mod_node=None, new_node=None, context_parent_path=None):
        self.type = type
        self.key_path = key_path # List of strings (key___index or key___INSERTED...)
        self.old_node = old_node
        self.mod_node = mod_node
        self.new_node = new_node # Instance from the original New AST
        self.context_parent_path = context_parent_path if context_parent_path is not None else []

    def __repr__(self):
        def _get_node_short_repr(node):
            if node is None: return "ABSENT"
            s = repr(node)
            if len(s) > 70: s = s[:33] + "..." + s[-34:]
            return s[1:-1] # Remove < >

        old_repr = f"O:{_get_node_short_repr(self.old_node)}"
        mod_repr = f"M:{_get_node_short_repr(self.mod_node)}"
        new_repr = f"N:{_get_node_short_repr(self.new_node)}"
        
        path_str = '.'.join(map(str,self.key_path)) if self.key_path else 'ROOT'
        parent_path_str = '.'.join(map(str,self.context_parent_path)) if self.context_parent_path else 'ROOT_PARENT'

        return (f"PdsChange(Type='{self.type:<45}', Path='{path_str}', \n"
                f"          ParentCtx='{parent_path_str}', \n"
                f"          Nodes=[\n            {old_repr},\n            {mod_repr},\n            {new_repr}\n          ])")

class PairwiseDiffResult:
    # UNCHANGED
    def __init__(self, change_type, path, old_item=None, new_item=None, old_idx=-1, new_idx=-1):
        self.change_type = change_type # 'IDENTICAL', 'MODIFIED', 'ADDED', 'DELETED'
        self.path = path # List of strings
        self.old_item = old_item
        self.new_item = new_item
        self.old_idx = old_idx # Index in its original list1
        self.new_idx = new_idx # Index in its original list2

    def __repr__(self):
        o_repr = f"'{getattr(self.old_item, 'key', type(self.old_item).__name__)}'" if self.old_item else "None"
        n_repr = f"'{getattr(self.new_item, 'key', type(self.new_item).__name__)}'" if self.new_item else "None"
        path_str = '.'.join(map(str, self.path))
        return (f"PairwiseDiff(type={self.change_type}, path={path_str}, "
                f"old={o_repr}@{self.old_idx}, new={n_repr}@{self.new_idx})")

class PdsDiffer:
    def __init__(self):
        pass # No state needed for now

    def _get_node_key_for_path(self, node: PdsNode):
        """Gets a string representation of the node's key for path construction."""
        if node is None: return "__NONE__" # Should not happen if node is from list
        
        # For PdsBlock, PdsKeyValuePair, PdsList, PdsOperatorCondition, use 'key' attribute
        if hasattr(node, 'key') and node.key is not None:
            # Sanitize key for path segment if it contains dots or triple underscores
            # This is a simple replacement; more robust might be URL encoding or similar.
            key_str = str(node.key)
            key_str = key_str.replace("___", "__^__") # Escape internal triple underscore
            key_str = key_str.replace(".", "^")      # Escape internal dot
            return key_str

        # For PdsComment, use a generic identifier, maybe hash of text for more uniqueness
        if isinstance(node, PdsComment): return f"__COMMENT_{hash(node.comment_text[:20])}"
        if isinstance(node, PdsBlankLine): return f"__BLANK_LINE_{node.line_number}" # Line number makes it unique-ish
        
        # Fallback for nodes without a .key (should be rare for structural elements)
        return f"__{node.__class__.__name__.upper()}_L{node.line_number}"

    def _are_nodes_structurally_equal(self, node1: PdsNode, node2: PdsNode):
        # This uses the PdsNode.__eq__ which performs a deep structural comparison by default.
        if node1 is None and node2 is None: return True
        if node1 is None or node2 is None: return False
        return node1 == node2 # Relies on PdsNode.__eq__

    def _perform_pairwise_diff(self, list1_nodes: list[PdsNode], list2_nodes: list[PdsNode], current_path_prefix: list[str]):
        pairwise_changes = []
        
        # SequenceMatcher needs hashable items. get_comparator_key provides these.
        # shallow_block_for_seq_matcher=True means for PdsBlock, only its key and opening comment are used
        # for this list alignment. Children are compared recursively.
        seq_match_items1 = [node.get_comparator_key(shallow_block_for_seq_matcher=True) for node in list1_nodes]
        seq_match_items2 = [node.get_comparator_key(shallow_block_for_seq_matcher=True) for node in list2_nodes]

        sm = difflib.SequenceMatcher(None, seq_match_items1, seq_match_items2, autojunk=False)

        # Keep track of how many times we've seen each key in list1 to generate unique indices for path
        # This is critical for `key___index` where index is among same-keyed items
        key_counts_list1 = {} 

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                for list1_idx_offset in range(i2 - i1):
                    original_list1_node_idx = i1 + list1_idx_offset
                    original_list2_node_idx = j1 + list1_idx_offset
                    
                    node1 = list1_nodes[original_list1_node_idx]
                    node2 = list2_nodes[original_list2_node_idx]
                    
                    node1_key_for_path = self._get_node_key_for_path(node1)
                    
                    # Determine the Nth occurrence of this key in list1 up to this point
                    # This is for the ___index part of the path
                    current_occurrence_idx = key_counts_list1.get(node1_key_for_path, 0)
                    key_counts_list1[node1_key_for_path] = current_occurrence_idx + 1
                    
                    path_segment = f"{node1_key_for_path}___{current_occurrence_idx}"
                    item_path = current_path_prefix + [path_segment]
                    
                    # `SequenceMatcher` says they are "equal" based on `get_comparator_key(shallow=True)`.
                    # Now do a full deep structural comparison. PdsNode.__eq__ handles this.
                    if self._are_nodes_structurally_equal(node1, node2):
                        change_type_for_this_node_itself = 'IDENTICAL'
                    else:
                        change_type_for_this_node_itself = 'MODIFIED'
                    
                    pairwise_changes.append(PairwiseDiffResult(
                        change_type_for_this_node_itself, 
                        item_path, node1, node2, 
                        original_list1_node_idx, original_list2_node_idx
                    ))
                    
                    # If they are blocks, ALWAYS recurse to find changes *within* their children,
                    # regardless of whether the block containers themselves were IDENTICAL or MODIFIED.
                    if isinstance(node1, PdsBlock) and isinstance(node2, PdsBlock):
                        pairwise_changes.extend(self._perform_pairwise_diff(node1.children, node2.children, item_path))
            
            elif tag == 'replace': # SequenceMatcher found completely different items at these aligned slots
                # Treat as DELETES from list1, then ADDS from list2
                for list1_idx_offset in range(i2 - i1): # Iterate over items being deleted from list1
                    original_list1_node_idx = i1 + list1_idx_offset
                    node_del = list1_nodes[original_list1_node_idx]
                    
                    node_del_key_for_path = self._get_node_key_for_path(node_del)
                    current_occurrence_idx = key_counts_list1.get(node_del_key_for_path, 0)
                    key_counts_list1[node_del_key_for_path] = current_occurrence_idx + 1
                    path_seg_del = f"{node_del_key_for_path}___{current_occurrence_idx}"
                    item_path_del = current_path_prefix + [path_seg_del]
                    
                    pairwise_changes.append(PairwiseDiffResult(
                        'DELETED', item_path_del, node_del, None, original_list1_node_idx, -1
                    ))

                for list2_idx_offset in range(j2 - j1): # Iterate over items being added from list2
                    original_list2_node_idx = j1 + list2_idx_offset
                    node_add = list2_nodes[original_list2_node_idx]
                    
                    node_add_key_for_path = self._get_node_key_for_path(node_add)
                    # For ADDED, path needs to indicate where it was inserted relative to list1's structure
                    # and its own key. The index is relative to list2.
                    # The `___INSERTED_at_L1idx_{i1}` gives context from list1.
                    # The index within same-keyed items in list2 is not explicitly part of this simple path segment.
                    # Let's use a simpler ADDED path for now, reconciler can refine.
                    # The key is enough to identify it, the context comes from i1.
                    path_seg_add = f"{node_add_key_for_path}___INSERTED_at_L1idx_{i1}_L2idx_{original_list2_node_idx}"
                    item_path_add = current_path_prefix + [path_seg_add]
                    
                    pairwise_changes.append(PairwiseDiffResult(
                        'ADDED', item_path_add, None, node_add, -1, original_list2_node_idx
                    ))
            
            elif tag == 'delete': # Item only in list1
                for list1_idx_offset in range(i2 - i1):
                    original_list1_node_idx = i1 + list1_idx_offset
                    node_del = list1_nodes[original_list1_node_idx]

                    node_del_key_for_path = self._get_node_key_for_path(node_del)
                    current_occurrence_idx = key_counts_list1.get(node_del_key_for_path, 0)
                    key_counts_list1[node_del_key_for_path] = current_occurrence_idx + 1
                    path_seg_del = f"{node_del_key_for_path}___{current_occurrence_idx}"
                    item_path_del = current_path_prefix + [path_seg_del]
                    
                    pairwise_changes.append(PairwiseDiffResult(
                        'DELETED', item_path_del, node_del, None, original_list1_node_idx, -1
                    ))

            elif tag == 'insert': # Item only in list2
                for list2_idx_offset in range(j2 - j1):
                    original_list2_node_idx = j1 + list2_idx_offset
                    node_add = list2_nodes[original_list2_node_idx]
                    
                    node_add_key_for_path = self._get_node_key_for_path(node_add)
                    path_seg_add = f"{node_add_key_for_path}___INSERTED_at_L1idx_{i1}_L2idx_{original_list2_node_idx}"
                    item_path_add = current_path_prefix + [path_seg_add]

                    pairwise_changes.append(PairwiseDiffResult(
                        'ADDED', item_path_add, None, node_add, -1, original_list2_node_idx
                    ))
        return pairwise_changes

    def diff_nodes(self, old_nodes_root: list[PdsNode], mod_nodes_root: list[PdsNode], new_nodes_root: list[PdsNode]):
        # Perform pairwise diffs
        mod_diffs_from_old = self._perform_pairwise_diff(old_nodes_root, mod_nodes_root, [])
        van_diffs_from_old = self._perform_pairwise_diff(old_nodes_root, new_nodes_root, [])

        # Reconcile into PdsChange objects
        # Create maps from path string to PairwiseDiffResult for easy lookup
        mod_map = {'.'.join(d.path): d for d in mod_diffs_from_old}
        van_map = {'.'.join(d.path): d for d in van_diffs_from_old}
        
        # Get all unique paths that appear in either diff, ensuring they are Old-centric
        all_old_centric_paths_str = set(mod_map.keys()) | set(van_map.keys())
        
        final_pds_changes = []

        for path_str in sorted(list(all_old_centric_paths_str)): # Sort for deterministic output
            path_list = path_str.split('.') # Path segments
            context_parent_path = path_list[:-1] # Path to the parent in Old structure

            mod_ch_pair = mod_map.get(path_str) # PairwiseDiffResult for Old vs Mod at this path
            van_ch_pair = van_map.get(path_str) # PairwiseDiffResult for Old vs New at this path

            o_node, m_node, n_node_from_new_ast = None, None, None
            
            # Determine old_node (must be consistent from either diff if path refers to existing Old item)
            if mod_ch_pair and mod_ch_pair.old_item:
                o_node = mod_ch_pair.old_item
            elif van_ch_pair and van_ch_pair.old_item:
                o_node = van_ch_pair.old_item
            
            # Determine mod_node and its status relative to old_node
            status_om = 'ABSENT_IN_MOD_DIFF' # Default if no specific diff entry for this path in Mod
            if mod_ch_pair: # A diff entry exists for this Old path in Mod
                status_om = mod_ch_pair.change_type # IDENTICAL, MODIFIED, ADDED, DELETED
                m_node = mod_ch_pair.new_item # This is the node from Mod AST
            elif o_node: 
                # Old node existed, but no specific diff entry means Mod kept Old's version
                status_om = 'IDENTICAL'
                m_node = o_node # Mod's version is structurally same as Old's
            # If o_node is None and no mod_ch_pair, it means this path is for an item
            # that was ADDED in Vanilla, and Mod didn't touch it. m_node remains None.


            # Determine new_node (from New AST) and its status relative to old_node
            status_on = 'ABSENT_IN_VANILLA_DIFF' # Default
            if van_ch_pair: # A diff entry exists for this Old path in Vanilla
                status_on = van_ch_pair.change_type
                n_node_from_new_ast = van_ch_pair.new_item # This is the instance from New AST
            elif o_node:
                # Old node existed, but no specific diff entry means Vanilla kept Old's version
                status_on = 'IDENTICAL'
                n_node_from_new_ast = o_node # Vanilla's version is structurally same as Old's.
                                           # This is a placeholder; we need the *actual* instance from New.
                                           # This case needs careful handling: if van_ch_pair is None but o_node existed,
                                           # it means the item was effectively DELETED by Vanilla relative to this Old path.
                                           # This part of the logic was flawed previously.
                # Correction: If o_node existed but van_ch_pair is None for that path,
                # it implies Vanilla deleted the item that was at o_node's path.
                status_on = 'DELETED' 
                n_node_from_new_ast = None # No corresponding node in New AST at this Old path
            # If o_node is None and no van_ch_pair, it means this path is for an item
            # that was ADDED in Mod, and Vanilla didn't touch it. n_node_from_new_ast remains None.


            # --- Refined Reconciliation Logic ---
            final_change_type = "UNHANDLED_RECONCILIATION_CASE"

            if status_om == 'IDENTICAL': # Mod made no change to o_node
                if status_on == 'IDENTICAL': continue # No change anywhere, skip.
                elif status_on == 'MODIFIED': final_change_type = 'VANILLA_MODIFIED'
                elif status_on == 'ADDED':    final_change_type = 'VANILLA_ADDED' # Vanilla added where Old was (or added something new if o_node=None)
                elif status_on == 'DELETED':  final_change_type = 'VANILLA_DELETED' # Vanilla deleted o_node
            
            elif status_om == 'MODIFIED': # Mod modified o_node
                if status_on == 'IDENTICAL':  final_change_type = 'MOD_MODIFIED'
                elif status_on == 'MODIFIED': # Both modified o_node
                    if self._are_nodes_structurally_equal(m_node, n_node_from_new_ast):
                        final_change_type = 'CONVERGED_MODIFICATION'
                    else:
                        final_change_type = 'CONFLICT_MODIFIED'
                elif status_on == 'ADDED': # Mod modified o_node, Vanilla added something new at same "slot" (unlikely if paths are specific)
                    final_change_type = 'ERROR_MOD_MODIFIED_VANILLA_ADDED_AT_SLOT' 
                elif status_on == 'DELETED': # Mod modified o_node, Vanilla deleted it
                    final_change_type = 'CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED'
            
            elif status_om == 'ADDED': # Mod added m_node (o_node was None or different)
                if status_on == 'IDENTICAL' or status_on == 'ABSENT_IN_VANILLA_DIFF' or status_on == 'DELETED':
                    # Vanilla either kept what was there (nothing, if o_node was None), 
                    # or Vanilla also deleted what o_node was. Mod's addition is clean.
                    final_change_type = 'MOD_ADDED'
                elif status_on == 'MODIFIED': # Vanilla modified o_node (which was None for Mod's ADD path basis?) - complex
                    final_change_type = 'ERROR_MOD_ADDED_VANILLA_MODIFIED_AT_SLOT'
                elif status_on == 'ADDED': # Both Mod and Vanilla added something at the same "slot"
                    if self._are_nodes_structurally_equal(m_node, n_node_from_new_ast):
                        final_change_type = 'MOD_ADDED_CONVERGED' # Both added the same thing
                    else:
                        final_change_type = 'CONFLICT_ADDITION' # Both added different things
            
            elif status_om == 'DELETED': # Mod deleted o_node
                if status_on == 'IDENTICAL':  final_change_type = 'MOD_DELETED'
                elif status_on == 'MODIFIED': # Mod deleted o_node, Vanilla modified it
                    final_change_type = 'CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED'
                elif status_on == 'ADDED': # Mod deleted o_node, Vanilla added something new there (unlikely for specific o_node path)
                    final_change_type = 'ERROR_MOD_DELETED_VANILLA_ADDED_AT_SLOT'
                elif status_on == 'DELETED': # Both deleted o_node
                    final_change_type = 'MOD_DELETED_VANILLA_ALSO_DELETED'

            elif status_om == 'ABSENT_IN_MOD_DIFF': # Path from Vanilla diff, Mod had no entry here
                if status_on == 'ADDED': final_change_type = 'VANILLA_ADDED'
                elif status_on == 'MODIFIED': final_change_type = 'VANILLA_MODIFIED' # Should not happen if o_node determined correctly
                # other status_on are less likely here if path is valid from van_map

            if final_change_type != "UNHANDLED_RECONCILIATION_CASE" and not final_change_type.startswith("ERROR_"):
                final_pds_changes.append(PdsChange(final_change_type, path_list,
                                                   o_node, m_node, n_node_from_new_ast,
                                                   context_parent_path))
            elif final_change_type.startswith("ERROR_") or final_change_type == "UNHANDLED_RECONCILIATION_CASE":
                sys.stderr.write(f"DEBUG: Differ unhandled/error: Type='{final_change_type}' Path: {path_str}\n"
                                 f"  OM:'{status_om}' (o:{o_node is not None}, m:{m_node is not None})\n"
                                 f"  ON:'{status_on}' (o:{o_node is not None}, n:{n_node_from_new_ast is not None})\n")
                      
        return final_pds_changes