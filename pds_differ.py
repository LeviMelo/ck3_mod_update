import difflib
import sys 
from pds_parser import PdsNode, PdsBlock, PdsComment, PdsBlankLine, PdsKeyValuePair, PdsList, PdsOperatorCondition

class PdsChange:
    # UNCHANGED from previous good version
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
    # UNCHANGED
    def __init__(self, change_type, path, old_item=None, new_item=None, old_idx=-1, new_idx=-1):
        self.change_type=change_type; self.path=path; self.old_item=old_item; self.new_item=new_item
        self.old_idx=old_idx; self.new_idx=new_idx
    def __repr__(self):
        o=f"'{getattr(self.old_item,'key',type(self.old_item).__name__)}'" if self.old_item else "None"
        n=f"'{getattr(self.new_item,'key',type(self.new_item).__name__)}'" if self.new_item else "None"
        return f"PairwiseDiff(type={self.change_type}, path={'.'.join(self.path)}, old={o}@{self.old_idx}, new={n}@{self.new_idx})"

class PdsDiffer:
    # IDENTICAL to the version from the prompt before last (the one that produced 41 changes)
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
        seq_match_items1 = [node.get_comparator_key(shallow_block_for_seq_matcher=True) for node in list1_nodes]
        seq_match_items2 = [node.get_comparator_key(shallow_block_for_seq_matcher=True) for node in list2_nodes]
        sm = difflib.SequenceMatcher(None, seq_match_items1, seq_match_items2, autojunk=False)
        key_counts_list1 = {} 
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                for idx_offset in range(i2 - i1):
                    l1_idx, l2_idx = i1 + idx_offset, j1 + idx_offset
                    n1, n2 = list1_nodes[l1_idx], list2_nodes[l2_idx]
                    n1_key = self._get_node_key_for_path(n1)
                    occurrence = key_counts_list1.get(n1_key, 0); key_counts_list1[n1_key] = occurrence + 1
                    path_seg = f"{n1_key}___{occurrence}"
                    item_path = current_path_prefix + [path_seg]
                    change_type = 'IDENTICAL' if self._are_nodes_structurally_equal(n1, n2) else 'MODIFIED'
                    pairwise_changes.append(PairwiseDiffResult(change_type, item_path, n1, n2, l1_idx, l2_idx))
                    if isinstance(n1, PdsBlock) and isinstance(n2, PdsBlock):
                        pairwise_changes.extend(self._perform_pairwise_diff(n1.children, n2.children, item_path))
            elif tag == 'replace':
                for k_del in range(i2 - i1):
                    l1_idx_del = i1 + k_del; node_del = list1_nodes[l1_idx_del]
                    n_del_key = self._get_node_key_for_path(node_del)
                    occ_del = key_counts_list1.get(n_del_key,0); key_counts_list1[n_del_key] = occ_del + 1
                    item_path_del = current_path_prefix + [f"{n_del_key}___{occ_del}"]
                    pairwise_changes.append(PairwiseDiffResult('DELETED', item_path_del, node_del, None, l1_idx_del, -1))
                for k_add in range(j2 - j1):
                    l2_idx_add = j1 + k_add; node_add = list2_nodes[l2_idx_add]
                    n_add_key = self._get_node_key_for_path(node_add)
                    item_path_add = current_path_prefix + [f"{n_add_key}___INSERTED_at_L1idx_{i1}_L2idx_{l2_idx_add}"]
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
                    item_path_add = current_path_prefix + [f"{n_add_key}___INSERTED_at_L1idx_{i1}_L2idx_{l2_idx}"]
                    pairwise_changes.append(PairwiseDiffResult('ADDED', item_path_add, None, node_add, -1, l2_idx))
        return pairwise_changes

    def diff_nodes(self, old_nodes_root: list[PdsNode], mod_nodes_root: list[PdsNode], new_nodes_root: list[PdsNode]):
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
            if mod_c: s_om = mod_c.change_type; m = mod_c.new_item
            elif o: s_om = 'IDENTICAL'; m = o
            if van_c: s_on = van_c.change_type; n = van_c.new_item
            elif o: s_on = 'DELETED'; n = None 
            f_type = "UNHANDLED_RECONCILIATION_CASE"
            if s_om == 'IDENTICAL':
                if s_on == 'IDENTICAL': continue
                elif s_on == 'MODIFIED': f_type = 'VANILLA_MODIFIED'
                elif s_on == 'ADDED':    f_type = 'VANILLA_ADDED'
                elif s_on == 'DELETED':  f_type = 'VANILLA_DELETED'
            elif s_om == 'MODIFIED':
                if s_on == 'IDENTICAL':  f_type = 'MOD_MODIFIED'
                elif s_on == 'MODIFIED': f_type = 'CONVERGED_MODIFICATION' if self._are_nodes_structurally_equal(m,n) else 'CONFLICT_MODIFIED'
                elif s_on == 'ADDED':    f_type = 'ERROR_MOD_MODIFIED_VANILLA_ADDED_AT_SLOT' 
                elif s_on == 'DELETED':  f_type = 'CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED'
            elif s_om == 'ADDED':
                if s_on == 'IDENTICAL' or s_on == 'ABSENT_IN_VANILLA_DIFF' or s_on == 'DELETED': f_type = 'MOD_ADDED'
                elif s_on == 'MODIFIED': f_type = 'ERROR_MOD_ADDED_VANILLA_MODIFIED_AT_SLOT'
                elif s_on == 'ADDED':    f_type = 'MOD_ADDED_CONVERGED' if self._are_nodes_structurally_equal(m,n) else 'CONFLICT_ADDITION'
            elif s_om == 'DELETED':
                if s_on == 'IDENTICAL':  f_type = 'MOD_DELETED'
                elif s_on == 'MODIFIED': f_type = 'CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED'
                elif s_on == 'ADDED':    f_type = 'ERROR_MOD_DELETED_VANILLA_ADDED_AT_SLOT'
                elif s_on == 'DELETED':  f_type = 'MOD_DELETED_VANILLA_ALSO_DELETED'
            elif s_om == 'ABSENT_IN_MOD_DIFF':
                if s_on == 'ADDED': f_type = 'VANILLA_ADDED'
                elif s_on == 'MODIFIED': f_type = 'VANILLA_MODIFIED'
            if f_type != "UNHANDLED_RECONCILIATION_CASE" and not f_type.startswith("ERROR_"):
                final_changes.append(PdsChange(f_type, p_list, o, m, n, ctx_path))
            elif f_type.startswith("ERROR_") or f_type == "UNHANDLED_RECONCILIATION_CASE":
                 sys.stderr.write(f"DEBUG: Differ unhandled/error: Type='{f_type}' Path: {path_str}\n  OM:'{s_om}' ON:'{s_on}'\n")
        return final_changes