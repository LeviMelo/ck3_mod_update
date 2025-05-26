# pds_differ.py (Final Diff Logic Refinement)
from pds_parser import PdsNode, PdsBlock, PdsKeyValuePair, PdsList, PdsComment, PdsBlankLine

class PdsChange:
    """Represents a detected difference/change."""
    def __init__(self, type, key_path, old_node=None, mod_node=None, new_node=None, context_parent_path=None):
        self.type = type 
        self.key_path = key_path 
        self.old_node = old_node
        self.mod_node = mod_node
        self.new_node = new_node
        self.context_parent_path = context_parent_path if context_parent_path is not None else []

    def __repr__(self):
        def _get_node_short_repr(node):
            if node is None:
                return "ABSENT"
            if isinstance(node, PdsKeyValuePair):
                val_display = str(node.value)
                if len(val_display) > 30: val_display = val_display[:27] + "..."
                return f"{node.key}='{val_display}'"
            if isinstance(node, PdsList):
                val_display = " ".join(node.values)
                if len(val_display) > 25: val_display = val_display[:22] + "..." 
                return f"{node.key}={{ {val_display} }}" 
            if isinstance(node, PdsBlock):
                return f"{node.key}={{...}}"
            if isinstance(node, PdsComment):
                text = node.comment_text if hasattr(node, 'comment_text') else node.raw_line.strip()
                return f"Comment:'{text[:20]}...'" if len(text) > 20 else f"Comment:'{text}'"
            if isinstance(node, PdsBlankLine):
                return "Blank"
            raw_strip = node.raw_line.strip()
            if raw_strip == "}": return "ClosingBrace"
            return f"Node('{raw_strip[:20]}...')"

        old_repr = f"O:{_get_node_short_repr(self.old_node)}"
        mod_repr = f"M:{_get_node_short_repr(self.mod_node)}"
        new_repr = f"N:{_get_node_short_repr(self.new_node)}"
        
        path_str = '.'.join(self.key_path) if self.key_path else 'ROOT'
        parent_path_str = '.'.join(self.context_parent_path) if self.context_parent_path else 'ROOT'

        return (f"PdsChange(Type='{self.type:<35}', Path='{path_str}', Parent='{parent_path_str}', \n"
                f"          Nodes=[{old_repr}, {mod_repr}, {new_repr}])")


class PdsDiffer:
    def __init__(self):
        pass

    def diff_nodes(self, old_nodes, mod_nodes, new_nodes, current_path=None):
        if current_path is None:
            current_path = []
        changes = []

        old_map = {self._get_node_identifier(node): node for node in old_nodes if node is not None}
        mod_map = {self._get_node_identifier(node): node for node in mod_nodes if node is not None}
        new_map = {self._get_node_identifier(node): node for node in new_nodes if node is not None}

        all_identifiers = sorted(list(set(old_map.keys()) | set(mod_map.keys()) | set(new_map.keys())))
        
        for identifier_idx, identifier in enumerate(all_identifiers):
            old_node = old_map.get(identifier)
            mod_node = mod_map.get(identifier)
            new_node = new_map.get(identifier)
            
            display_key_for_path = self._get_display_key(identifier, old_node, mod_node, new_node, identifier_idx)
            node_key_path = current_path + [display_key_for_path]

            status_O_M = self._compare_presence_and_content(old_node, mod_node)
            status_O_N = self._compare_presence_and_content(old_node, new_node)
            status_M_N = self._compare_presence_and_content(mod_node, new_node) # For direct M vs N comparison

            change_type = None

            # --- Prioritized Change Detection Logic ---

            # 1. ADDED by Mod (not in O, in M)
            if status_O_M == 'ADDED':
                if status_O_N == 'ADDED': # Also added by New Vanilla (converged or conflicting)
                    if status_M_N == 'IDENTICAL':
                        change_type = 'MOD_ADDED_CONVERGED'
                    else:
                        change_type = 'CONFLICT_ADDITION' # Mod and NV added different versions
                else: # Only Mod added it (not in New Vanilla relative to Old)
                    change_type = 'MOD_ADDED'
            
            # 2. ADDED by Vanilla (not in O, not in M, but in N)
            elif status_O_N == 'ADDED': # If not caught by MOD_ADDED above, then this is a pure NV add
                change_type = 'VANILLA_ADDED'

            # --- Remaining cases where node was present in Old (O) ---

            # 3. DELETED by Mod (in O, not in M)
            elif status_O_M == 'DELETED':
                if status_O_N == 'DELETED': # Also deleted by New Vanilla
                    change_type = 'MOD_DELETED_VANILLA_ALSO_DELETED'
                elif status_O_N == 'MODIFIED': # NV modified it
                    change_type = 'CONFLICT_DELETION' # Mod deleted, NV modified
                elif status_O_N == 'IDENTICAL': # NV did not change it
                    change_type = 'MOD_DELETED'

            # 4. DELETED by Vanilla (in O, not in N, and not already handled by MOD_DELETED)
            elif status_O_N == 'DELETED':
                # This branch implies MOD_DELETED (status_O_M == 'DELETED') was FALSE
                # So if Mod is still present in M, it's a conflict or only NV deleted.
                if status_O_M == 'MODIFIED': # Mod modified it
                    change_type = 'CONFLICT_DELETION' # NV deleted, Mod modified
                elif status_O_M == 'IDENTICAL': # Mod did not change it
                    change_type = 'VANILLA_DELETED'

            # 5. MODIFIED by Mod (in O, in M, different, and not caught by deletion/addition conflicts)
            elif status_O_M == 'MODIFIED':
                if status_O_N == 'MODIFIED': # Also modified by Vanilla (converged or conflicting)
                    if status_M_N == 'IDENTICAL': # Both modified to the same new state
                        change_type = 'CONVERGED_MODIFICATION'
                    else: # Both modified to different states
                        change_type = 'CONFLICT_MODIFIED'
                elif status_O_N == 'IDENTICAL': # Only Mod modified (NV is same as O)
                    change_type = 'MOD_MODIFIED'

            # 6. MODIFIED by Vanilla (in O, in N, different, and not caught by prior modified/deletion conflicts)
            elif status_O_N == 'MODIFIED': # If not caught by MOD_MODIFIED above, then pure NV mod
                change_type = 'VANILLA_MODIFIED'
            
            # 7. IDENTICAL (All three are the same or not a change)
            else: # status_O_M == 'IDENTICAL' and status_O_N == 'IDENTICAL'
                pass # No change to report

            if change_type:
                changes.append(PdsChange(change_type, node_key_path, old_node, mod_node, new_node, current_path))

            # --- Recursively Diff Children (for Blocks) ---
            if (isinstance(old_node, PdsBlock) and 
                isinstance(mod_node, PdsBlock) and 
                isinstance(new_node, PdsBlock)):
                sub_changes = self.diff_nodes(old_node.children, mod_node.children, new_node.children, node_key_path)
                changes.extend(sub_changes)

        return changes

    # Helper methods remain the same (no changes to: _get_node_identifier, _get_display_key, _are_nodes_structurally_different, _compare_presence_and_content)
    def _get_node_identifier(self, node):
        if node is None: return None
        if hasattr(node, 'key'):
            return node.key
        if isinstance(node, PdsComment):
            return f"__COMMENT_L{node.line_number}_{hash(node.comment_text if hasattr(node, 'comment_text') else node.raw_line)}"
        if isinstance(node, PdsBlankLine):
            return f"__BLANK_L{node.line_number}"
        return f"__UNKEYED_L{node.line_number}_{hash(node.raw_line.strip())}"

    def _get_display_key(self, identifier_from_map, old_node, mod_node, new_node, index_in_all_identifiers):
        for node in [old_node, mod_node, new_node]:
            if node and hasattr(node, 'key'):
                return node.key
        
        display_node = next((n for n in [old_node, mod_node, new_node] if n is not None), None)
        if display_node:
            if isinstance(display_node, PdsComment):
                return f"Comment@L{display_node.line_number}"
            if isinstance(display_node, PdsBlankLine):
                return f"BlankLine@L{display_node.line_number}"
            raw_strip = display_node.raw_line.strip()
            if raw_strip == "}": return "ClosingBrace"
            return f"Item@{display_node.line_number}({raw_strip[:15].replace('.', '_')})"
        
        if isinstance(identifier_from_map, str) and not identifier_from_map.startswith("__"):
            return identifier_from_map

        return f"ItemAtIndex_{index_in_all_identifiers}"

    def _are_nodes_structurally_different(self, node1, node2):
        if type(node1) != type(node2): return True
        if isinstance(node1, PdsKeyValuePair):
            return (node1.key != node2.key or str(node1.value) != str(node2.value) or node1.comment_text_on_line != node2.comment_text_on_line)
        elif isinstance(node1, PdsList):
            return (node1.key != node2.key or node1.values != node2.values or node1.comment_text_on_line != node2.comment_text_on_line)
        elif isinstance(node1, PdsBlock):
            return (node1.key != node2.key or node1.comment_text_on_line != node2.comment_text_on_line)
        elif isinstance(node1, PdsComment):
            return node1.comment_text != node2.comment_text
        elif isinstance(node1, PdsBlankLine):
            return False
        if hasattr(node1, 'raw_line') and hasattr(node2, 'raw_line'):
            return node1.raw_line.strip() != node2.raw_line.strip()
        return False

    def _compare_presence_and_content(self, node_ref, node_comp):
        if node_ref is None and node_comp is None: return 'ABSENT'
        if node_ref is None and node_comp is not None: return 'ADDED'
        if node_ref is not None and node_comp is None: return 'DELETED'
        
        if self._are_nodes_structurally_different(node_ref, node_comp):
            return 'MODIFIED'
        else:
            return 'IDENTICAL'