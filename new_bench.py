import os
import sys
import difflib
import collections
import re
import copy # For deep copying AST nodes

# --- Python Module Imports ---
from pds_parser import (
    PdsNode, PdsKeyValuePair, PdsBlock, PdsList,
    PdsComment, PdsBlankLine, PdsOperatorCondition,
    PdsParser
)
from pds_differ import (
    PdsDiffer, PdsChange,
    _find_node_by_diff_path_in_tree,
    get_node_diff_key_for_find
)

print(f"--- Successfully imported PdsNode, PdsParser, PdsDiffer definitions ---")
print("-" * 80)

# --- Benchmark File Paths ---
BENCHMARK_DIR = os.path.join(os.getcwd(), "benchmark_files")
BENCHMARK_OLD = os.path.join(BENCHMARK_DIR, "benchmark_test_old.txt")
BENCHMARK_MOD = os.path.join(BENCHMARK_DIR, "benchmark_test_mod.txt")
BENCHMARK_NEW = os.path.join(BENCHMARK_DIR, "benchmark_test_new.txt")
BENCHMARK_IDEAL_MERGED = os.path.join(BENCHMARK_DIR, "ideal_merged_benchmark.txt") # <-- NEW

# --- File I/O and Normalization Helpers (Unchanged) ---
def get_file_content(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f: return f.read()
    except UnicodeDecodeError:
        try:
            with open(filepath, 'r', encoding='utf-8') as f: return f.read()
        except Exception as e_inner: sys.stderr.write(f"ERROR reading {filepath} (fallback): {e_inner}\n"); return None
    except FileNotFoundError: sys.stderr.write(f"WARNING: File not found: {filepath}\n"); return None
    except Exception as e: sys.stderr.write(f"ERROR reading {filepath}: {e}\n"); return None

def write_to_file(filepath, content):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except Exception as e: sys.stderr.write(f"ERROR writing to {filepath}: {e}\n"); return False

def normalize_for_comparison(text):
    if not text: return ""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'\n(\s*\n)+', '\n\n', text)
    lines = [line.rstrip() for line in text.split('\n')]
    while lines and lines[0] == "": lines.pop(0)
    while lines and lines[-1] == "": lines.pop()
    text = '\n'.join(lines)
    return re.sub(r'\n\n+', '\n\n', text).strip()

# --- Helper to Strip "SimMerge:" Annotations ---
def _strip_simmerge_from_comment_text(comment_str):
    if comment_str is None: return None
    # This regex attempts to remove "SimMerge:TYPE" and associated suffixes,
    # preserving preceding original comment text.
    # It handles "SimMerge:" at the end of a line, possibly after a '#'.
    # Example: "Original comment # SimMerge:FOO_SUFFIX" -> "Original comment"
    # Example: "# SimMerge:FOO" -> None (or empty if original was just #)
    # Example: "SimMerge:FOO" (if comment_text_on_line directly holds it) -> None
    
    # Pattern: Optional leading stuff, then (optional '#') SimMerge:TAG possibly with _SUFFIXES, until end of string.
    # We want to capture the part *before* this.
    # (.*?): Captures the original comment part (non-greedy).
    # (\s*(#\s*)?SimMerge:[\w:]+(_[\w:]+)*\s*$) : Matches the SimMerge tag at the end.
    
    match = re.search(r'^(.*?)(\s*(#\s*)?SimMerge:[\w:]+(_[\w:]+)*\s*)$', comment_str)
    if match:
        original_part = match.group(1).strip()
        return original_part if original_part else None
    return comment_str # No SimMerge tag found at the end

class SimMergeStripper:
    def strip_node_annotations(self, node_original):
        if not isinstance(node_original, PdsNode): # Handle primitives in lists
            return node_original
            
        node = copy.deepcopy(node_original)

        if hasattr(node, 'comment_text_on_line'):
            node.comment_text_on_line = _strip_simmerge_from_comment_text(node.comment_text_on_line)

        if isinstance(node, PdsBlock):
            new_children = []
            for child_original in node.children:
                stripped_child = self.strip_node_annotations(child_original) # Recurse
                if isinstance(stripped_child, PdsComment):
                    # If PdsComment's own text was purely a SimMerge tag and got stripped to None/empty
                    if not stripped_child.comment_text:
                        continue # Discard this purely SimMerge comment node
                if stripped_child is not None: # Ensure recursion didn't nullify it entirely
                    new_children.append(stripped_child)
            node.children = new_children
        elif isinstance(node, PdsList):
            new_values = []
            for val_original in node.values:
                stripped_val = self.strip_node_annotations(val_original) # Recurse
                if isinstance(stripped_val, PdsComment):
                     if not stripped_val.comment_text:
                         continue
                if stripped_val is not None:
                    new_values.append(stripped_val)
            node.values = new_values
        elif isinstance(node, PdsComment): # For top-level comments or comments as direct list items
            node.comment_text = _strip_simmerge_from_comment_text(node.comment_text)
            if not node.comment_text: # If the entire comment was a SimMerge tag
                return None # Signal to remove this node
        
        # KVP and OperatorCondition values that are blocks/lists are handled by recursion on children/values.
        return node

def strip_simmerge_comments_from_ast_list(nodes_list_original: list) -> list:
    if not nodes_list_original: return []
    
    stripper = SimMergeStripper()
    processed_nodes_list = []

    for node_original_copy in [copy.deepcopy(n) for n in nodes_list_original]: # Work on copies
        processed_node = stripper.strip_node_annotations(node_original_copy)
        if processed_node is not None: # Node might be None if it was a PdsComment stripped entirely
            processed_nodes_list.append(processed_node)
            
    return processed_nodes_list


# --- Modified Assertion Function for SimMerge Annotations & Original Comments ---
# (Using the one from my previous "thought" block, which is an adaptation of your original)
def assert_final_merged_state(actual_simulated_nodes_with_comments, test_name_for_output=""):
    print(f"\n--- Asserting ANNOTATIONS and specific value outcomes for '{test_name_for_output}' on ACTUAL SIMULATED AST ---")
    results = {"pass": 0, "fail": 0, "skipped_assertions": 0}

    def _assert_node(path_segments, expected_value=None, expected_type=None,
                     check_simmerge_tag=None, check_original_comment=None,
                     is_absent=False, operator_check=None, list_content_check_set=None,
                     path_is_direct_key=False): # New flag

        path_str = '->'.join(map(str,path_segments))
        node = None
        
        if path_is_direct_key and len(path_segments) == 1: # Find by top-level key directly
            key_to_find = path_segments[0]
            node = next((n for n in actual_simulated_nodes_with_comments if hasattr(n, 'key') and n.key == key_to_find), None)
            if not node and (expected_type == PdsComment or (check_original_comment and not hasattr(PdsNode, 'key'))): # For comments by text
                 node = next((n for n in actual_simulated_nodes_with_comments if isinstance(n, PdsComment) and getattr(n, 'comment_text', '') == expected_value), None)

        else: # Find by diff path
            node = _find_node_by_diff_path_in_tree(actual_simulated_nodes_with_comments, path_segments)


        current_success = True
        messages = []

        if is_absent:
            if node is None:
                messages.append(f"  PASS: Node at '{path_str}' is correctly absent.")
            else:
                messages.append(f"  FAIL: Node at '{path_str}' was found ({node!r}), but expected to be absent.")
                current_success = False
        elif node is None:
            messages.append(f"  FAIL: Node not found at path '{path_str}'. Cannot perform further checks.")
            current_success = False
            results["fail"] += 1 # Count as fail immediately
            for msg in messages: print(msg)
            return current_success # Early exit for this assertion
        else:
            # Type Check
            if expected_type and not isinstance(node, expected_type):
                messages.append(f"  FAIL: Node at '{path_str}' type. Expected {expected_type.__name__}, Got {type(node).__name__}.")
                current_success = False

            # Value Check
            actual_node_value = None
            if isinstance(node, (PdsKeyValuePair, PdsOperatorCondition)): actual_node_value = node.value
            elif isinstance(node, PdsComment): actual_node_value = node.comment_text

            if expected_value is not None and not list_content_check_set:
                if isinstance(node, PdsBlock):
                    if node.key != expected_value:
                         messages.append(f"  FAIL: Block key at '{path_str}'. Expected '{expected_value}', Got '{node.key}'.")
                         current_success = False
                elif actual_node_value != expected_value:
                    messages.append(f"  FAIL: Value at '{path_str}'. Expected '{expected_value}', Got '{actual_node_value}'.")
                    current_success = False

            if operator_check and isinstance(node, PdsOperatorCondition):
                if node.operator != operator_check:
                    messages.append(f"  FAIL: Operator at '{path_str}'. Expected '{operator_check}', Got '{node.operator}'.")
                    current_success = False

            if list_content_check_set and isinstance(node, PdsList):
                # Convert PdsBlock items in list to their keys for simple comparison
                actual_list_comps = set()
                for v_item in node.values:
                    if isinstance(v_item, PdsBlock) and v_item.key is not None: actual_list_comps.add(str(v_item.key)) # Use string of key
                    elif isinstance(v_item, PdsBlock) and v_item.key is None: actual_list_comps.add("ANON_BLOCK") # Placeholder
                    elif isinstance(v_item, PdsNode): actual_list_comps.add(str(v_item.get_structural_components(shallow_block=True))) # Fallback for other nodes
                    else: actual_list_comps.add(str(v_item))

                expected_list_str_set = set(str(x) for x in list_content_check_set)
                if actual_list_comps != expected_list_str_set:
                    messages.append(f"  FAIL: List content (set) mismatch at '{path_str}'.\n  Expected (any order): {expected_list_str_set}\n  Got: {actual_list_comps}")
                    current_success = False

            # SimMerge Tag Check
            if check_simmerge_tag:
                tag_found_on_line = False
                tag_found_in_child_comment = False
                node_line_comment = getattr(node, 'comment_text_on_line', None)
                if node_line_comment and check_simmerge_tag in node_line_comment:
                    tag_found_on_line = True

                if isinstance(node, PdsBlock):
                    for child_comment_node in node.children:
                        if isinstance(child_comment_node, PdsComment) and \
                           child_comment_node.comment_text and \
                           check_simmerge_tag in child_comment_node.comment_text:
                            tag_found_in_child_comment = True
                            break
                
                if not (tag_found_on_line or tag_found_in_child_comment):
                    messages.append(f"  FAIL: SimMerge tag '{check_simmerge_tag}' NOT found for '{path_str}'. Line comment: '{node_line_comment}'.")
                    current_success = False

            # Original Comment Check
            if check_original_comment:
                original_found = False
                # Check line comment of the node itself
                line_comment = getattr(node, 'comment_text_on_line', None)
                if line_comment:
                    stripped_line_comment = _strip_simmerge_from_comment_text(line_comment)
                    if stripped_line_comment and check_original_comment in stripped_line_comment:
                        original_found = True

                # If node is a PdsComment, check its own text
                if not original_found and isinstance(node, PdsComment):
                    stripped_main_comment = _strip_simmerge_from_comment_text(node.comment_text)
                    if stripped_main_comment and check_original_comment in stripped_main_comment:
                        original_found = True
                
                # If node is a PdsBlock, check its children comments
                if not original_found and isinstance(node, PdsBlock):
                    for child_node in node.children:
                        if isinstance(child_node, PdsComment):
                            stripped_child_comment = _strip_simmerge_from_comment_text(child_node.comment_text)
                            if stripped_child_comment and check_original_comment in stripped_child_comment:
                                original_found = True
                                break
                if not original_found:
                    messages.append(f"  FAIL: Original comment substring '{check_original_comment}' NOT found (after stripping SimMerge) for '{path_str}'. Line: '{line_comment}' Main: '{getattr(node, 'comment_text', 'N/A')}'")
                    current_success = False

        if not messages and current_success: # If all checks passed for this node
             messages.append(f"  PASS: Node at '{path_str}' meets annotation/value expectations.")

        for msg in messages:
            print(msg)

        if current_success: results["pass"] += 1
        else: results["fail"] += 1
        return current_success
    
    # --- Section 1 ---
    _assert_node(['primitive_string___0'], expected_value="mod_string_changed", expected_type=PdsKeyValuePair, check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")
    _assert_node(['primitive_number___0'], expected_value=999.9, expected_type=PdsKeyValuePair, check_simmerge_tag="SimMerge:MOD_MODIFIED")
    _assert_node(['primitive_bool___0'], expected_value=False, expected_type=PdsKeyValuePair, check_simmerge_tag="SimMerge:MOD_MODIFIED")
    _assert_node(['primitive_float___0'], expected_value=2.0, expected_type=PdsKeyValuePair, check_simmerge_tag="SimMerge:VANILLA_MODIFIED", check_original_comment="Vanilla changes float")

    # --- Section 2 ---
    _assert_node(['delete_mod_modify_new'], path_is_direct_key=True, expected_type=PdsBlock, check_simmerge_tag="SimMerge:CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED_VANILLA_MOD_KEPT", check_original_comment="Comment New")
    _assert_node(['delete_mod_modify_new', 'value___0'], expected_value=2) # Path relative to found block for children
    
    _assert_node(['add_mod_only'], path_is_direct_key=True, expected_value="mod_added", check_simmerge_tag="SimMerge:MOD_ADDED", check_original_comment="Comment Mod Added")
    _assert_node(['delete_new_only'], path_is_direct_key=True, is_absent=True)
    _assert_node(['modify_mod_delete_new'], path_is_direct_key=True, expected_value="modded_value", check_simmerge_tag="SimMerge:CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED_MOD_MOD_APPLIED_AS_ADD", check_original_comment="Comment Mod Modified")
    _assert_node(['delete_mod_keep_new'], path_is_direct_key=True, expected_value="original", check_simmerge_tag="SimMerge:MOD_DELETED")
    _assert_node(['keep_identical'], path_is_direct_key=True, expected_value="unchanged", check_original_comment="Comment Identical") # No SimMerge tag

    # Comment conflict, find by text then assert tag
    _assert_node(["This is a modded comment"], path_is_direct_key=True, expected_type=PdsComment, expected_value="This is a modded comment", check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")
    _assert_node(["# Mod replaced blank with comment"], path_is_direct_key=True, expected_type=PdsComment, expected_value="# Mod replaced blank with comment", check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")

    # --- Section 3: Lists ---
    _assert_node(['simple_list_conflict'], path_is_direct_key=True, expected_type=PdsList,
                 check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_ITEMS_MERGED_UNION",
                 check_original_comment="Vanilla changed b->item_v",
                 list_content_check_set={"item_a", "item_v", "item_e", "item_added_vanilla", "item_mod", "item_d", "item_added_mod"})

    _assert_node(['multi_line_list'], path_is_direct_key=True, expected_type=PdsList,
                 check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_ITEMS_MERGED_UNION",
                 list_content_check_set={"item_1", "item_2", "item_vanilla_added", "item_mod_added"})

    _assert_node(['list_with_anon_blocks'], path_is_direct_key=True, expected_type=PdsList,
                 check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_ITEMS_MERGED_UNION",
                 check_original_comment="Vanilla adds new anon block")
    # Deeper checks for list_with_anon_blocks items would require finding specific anonymous blocks, which is complex.
    # The content check above for the ideal AST is more robust for list item content.

    # --- Section 4: Blocks ---
    sbc_path_key = 'simple_block_conflict'
    _assert_node([sbc_path_key], path_is_direct_key=True, expected_type=PdsBlock,
                 check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN",
                 check_original_comment="Vanilla adds comment to block")
    _assert_node([sbc_path_key, 'child_kvp___0'], expected_value="mod_value_changed", check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")
    _assert_node([sbc_path_key, 'child_list___0'], expected_type=PdsList, check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_ITEMS_MERGED_UNION")
    _assert_node([sbc_path_key, 'mod_added_child___0'], expected_value=True, check_simmerge_tag="SimMerge:MOD_ADDED")

    pb_path_key = 'parent_block'
    _assert_node([pb_path_key], path_is_direct_key=True, expected_type=PdsBlock,
                 check_simmerge_tag="SimMerge:VANILLA_MODIFIED", # This tag might be on the block if its comment or a direct primitive child changed.
                 check_original_comment="Vanilla adds block comment")

    _assert_node([pb_path_key, 'child_block_level1_mod___0'], expected_type=PdsBlock,
                 check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN",
                 check_original_comment="Vanilla adds comment")
    _assert_node([pb_path_key, 'child_block_level1_mod___0', 'child_block_level2___0', 'final_value___0'],
                 expected_value="modded", check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")
    _assert_node([pb_path_key, 'child_block_level1_new___0'],
                 expected_type=PdsBlock, check_simmerge_tag="SimMerge:MOD_DELETED") # Mod deleted, Vanilla kept

    _assert_node(['modify_mod_delete_new_block'], path_is_direct_key=True, expected_type=PdsBlock,
                 check_simmerge_tag="SimMerge:CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED_MOD_MOD_APPLIED_AS_ADD")
    _assert_node(['delete_mod_modify_new_block'], path_is_direct_key=True, expected_type=PdsBlock,
                 check_simmerge_tag="SimMerge:CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED_VANILLA_MOD_KEPT",
                 check_original_comment="Comment DelModBlock New")

    # --- Section 5: Operator Conditions ---
    ce_path_key = 'condition_example'
    _assert_node([ce_path_key], path_is_direct_key=True, expected_type=PdsBlock,
                 check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN",
                 check_original_comment="Vanilla adds to block comment line")
    _assert_node([ce_path_key, 'base_value___0'], expected_value=10, operator_check=">=",
                 expected_type=PdsOperatorCondition, check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN",
                 check_original_comment="Mod changes operator and value") # Original from Mod here
    _assert_node([ce_path_key, 'always_true___0'], expected_value=False,
                 expected_type=PdsKeyValuePair, check_simmerge_tag="SimMerge:VANILLA_MODIFIED",
                 check_original_comment="Vanilla changes this KVP's value") # Original from New here

    # --- Section 6: Edge Cases ---
    _assert_node(['kvp_to_block'], path_is_direct_key=True, expected_type=PdsKeyValuePair,
                 check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN_TYPE_CHANGE",
                 check_original_comment="Comment KVPToBlock Mod")
    _assert_node(['block_key_change_old'], path_is_direct_key=True, is_absent=True)
    _assert_node(['block_key_change_mod'], path_is_direct_key=True, expected_type=PdsBlock,
                 check_simmerge_tag="SimMerge:MOD_ADDED", check_original_comment="Comment KeyChange Mod")
    _assert_node(['block_key_change_new'], path_is_direct_key=True, expected_type=PdsBlock,
                 check_simmerge_tag="SimMerge:VANILLA_ADDED", check_original_comment="Comment KeyChange New")
    _assert_node(['empty_block'], path_is_direct_key=True, expected_type=PdsBlock,
                 check_simmerge_tag="SimMerge:MOD_MODIFIED",
                 check_original_comment="Mod adds comment to empty block") # Original from Mod
    _assert_node(['block_with_comments'], path_is_direct_key=True, expected_type=PdsBlock,
                 check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN",
                 check_original_comment="Vanilla adds to block comment") # Original from New

    # --- Section 7: Random List ---
    rl_path_key = 'random_list_benchmark'
    _assert_node([rl_path_key], path_is_direct_key=True, expected_type=PdsList,
                 check_simmerge_tag="SimMerge:CONFLICT_MODIFIED_ITEMS_MERGED_UNION",
                 check_original_comment="Vanilla adds to block comment line") # Original from New
    # Specific items within random_list_benchmark are harder to assert tags on without complex pathing
    # The ideal AST content check is more crucial here.

    print(f"--- Annotation & Specific Outcome Assertions Complete for '{test_name_for_output}': {results['pass']} passed, {results['fail']} failed, {results['skipped_assertions']} skipped ---")
    return results["fail"] == 0


# --- Main Test Runner ---
def run_new_benchmark_test(test_name, old_path, mod_path, new_path, ideal_merged_path):
    print(f"\n{'='*20} RUNNING NEW BENCHMARK TEST: {test_name} {'='*20}")

    safe_test_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in test_name).replace(" ", "_")
    test_output_dir = os.path.join(os.getcwd(), "test_output", "new_benchmark_file_based", safe_test_name)
    os.makedirs(test_output_dir, exist_ok=True)
    print(f"Output files will be saved to: {test_output_dir}")

    # 1. Parse Input Files
    parser = PdsParser()
    old_nodes = parser.parse_file(old_path) if os.path.exists(old_path) else []
    mod_nodes = parser.parse_file(mod_path) if os.path.exists(mod_path) else []
    new_nodes = parser.parse_file(new_path) if os.path.exists(new_path) else []

    # 2. Parse Ideal Merged AST File (Content Only)
    print("\n--- Parsing Ideal Merged AST File (Content Only) ---")
    ideal_ast_content_nodes = parser.parse_file(ideal_merged_path) if os.path.exists(ideal_merged_path) else []
    if not ideal_ast_content_nodes and os.path.exists(ideal_merged_path):
        print(f"WARNING: Ideal merged file {ideal_merged_path} parsed to empty AST. Check file content and parser.")
    elif not os.path.exists(ideal_merged_path):
        print(f"ERROR: Ideal merged file {ideal_merged_path} not found. Cannot proceed with content comparison.")
        return # Cannot run test without ideal
        
    ideal_ast_content_str = PdsParser._nodes_to_string(ideal_ast_content_nodes)
    write_to_file(os.path.join(test_output_dir, "PARSED_IDEAL_MERGED_CONTENT.txt"), ideal_ast_content_str)
    print(f"Ideal merged content AST parsed ({len(ideal_ast_content_nodes)} root nodes).")

    # 3. Run PdsDiffer
    print("\n--- Running PdsDiffer: diff_nodes and simulate_three_way_merge ---")
    differ = PdsDiffer()
    if 'g_subsumed_mod_block_paths_for_simulation' in sys.modules['pds_differ'].__dict__:
         sys.modules['pds_differ'].__dict__['g_subsumed_mod_block_paths_for_simulation'] = set()

    changes = differ.diff_nodes(old_nodes, mod_nodes, new_nodes)
    with open(os.path.join(test_output_dir, "detected_3way_changes.txt"), 'w', encoding='utf-8') as f_changes:
        for chg in changes: f_changes.write(repr(chg) + "\n")
    print(f"PdsDiffer found {len(changes)} 3-way changes.")

    actual_simulated_ast_with_tags = differ.simulate_three_way_merge(changes, new_nodes)
    actual_simulated_ast_with_tags_str = PdsParser._nodes_to_string(actual_simulated_ast_with_tags)
    write_to_file(os.path.join(test_output_dir, "ACTUAL_SIMULATED_MERGED_WITH_ANNOTATIONS.txt"), actual_simulated_ast_with_tags_str)
    print(f"PdsDiffer generated simulated merge ({len(actual_simulated_ast_with_tags)} root nodes).")

    # 4. Content Comparison (Ideal AST vs. Actual Stripped AST)
    print("\n--- Comparing Merged Content (Ideal vs. Actual Stripped) ---")
    actual_simulated_ast_stripped = strip_simmerge_comments_from_ast_list(actual_simulated_ast_with_tags)
    actual_simulated_ast_stripped_str = PdsParser._nodes_to_string(actual_simulated_ast_stripped)
    write_to_file(os.path.join(test_output_dir, "ACTUAL_SIMULATED_MERGED_CONTENT_STRIPPED.txt"), actual_simulated_ast_stripped_str)

    normalized_ideal_str = normalize_for_comparison(ideal_ast_content_str)
    normalized_actual_stripped_str = normalize_for_comparison(actual_simulated_ast_stripped_str)

    content_diff_path = os.path.join(test_output_dir, "CONTENT_DIFF_IDEAL_VS_ACTUAL_STRIPPED_NORMALIZED.diff")
    content_match = True
    if normalized_ideal_str == normalized_actual_stripped_str:
        print("  PASS: Normalized content of Ideal Merge matches Actual Stripped Merge.")
        if os.path.exists(content_diff_path): os.remove(content_diff_path)
    else:
        print("  FAIL: Normalized content of Ideal Merge MISMATCHES Actual Stripped Merge!")
        content_match = False
        diff_lines = list(difflib.unified_diff(
            normalized_ideal_str.splitlines(keepends=True),
            normalized_actual_stripped_str.splitlines(keepends=True),
            fromfile="IDEAL_MERGED_CONTENT_NORMALIZED.txt",
            tofile="ACTUAL_SIMULATED_MERGED_CONTENT_STRIPPED_NORMALIZED.txt",
            lineterm=""
        ))
        with open(content_diff_path, 'w', encoding='utf-8') as f_diff: f_diff.writelines(diff_lines)
        print(f"  Normalized diff saved to: {os.path.basename(content_diff_path)}")
        
        # Optional: Raw string diff if normalized ones differ (can show subtle parsing/to_string issues)
        if ideal_ast_content_str != actual_simulated_ast_stripped_str:
            print("  NOTE: Non-normalized (raw to_string) content also differs.")
            raw_content_diff_path = os.path.join(test_output_dir, "CONTENT_DIFF_IDEAL_VS_ACTUAL_STRIPPED_RAW.diff")
            raw_diff_lines = list(difflib.unified_diff(
                ideal_ast_content_str.splitlines(keepends=True),
                actual_simulated_ast_stripped_str.splitlines(keepends=True),
                fromfile="IDEAL_MERGED_CONTENT_RAW.txt",
                tofile="ACTUAL_SIMULATED_MERGED_CONTENT_STRIPPED_RAW.txt",
                lineterm=""
            ))
            with open(raw_content_diff_path, 'w', encoding='utf-8') as f_raw_diff: f_raw_diff.writelines(raw_diff_lines)
            print(f"  Raw string diff saved to: {os.path.basename(raw_content_diff_path)}")


    # 5. Annotation and Specific Outcome Assertion (on the AST with tags)
    annotations_pass = assert_final_merged_state(actual_simulated_ast_with_tags, test_name)

    print(f"\n--- Test Summary for '{test_name}' ---")
    print(f"  Content Merge Test: {'PASS' if content_match else 'FAIL'}")
    print(f"  Annotation & Specific Outcome Test: {'PASS' if annotations_pass else 'FAIL'}")
    print("-" * 80)

# --- Entry Point ---
if __name__ == "__main__":
    # Create the ideal_merged_benchmark.txt file if it doesn't exist
    # (You should create this file manually with the content provided above)
    if not os.path.exists(BENCHMARK_IDEAL_MERGED):
        print(f"WARNING: Ideal merge file '{BENCHMARK_IDEAL_MERGED}' not found.")
        print("Please create it with the expected merged content (no SimMerge tags).")
        # As a fallback for running, let's write the generated ideal content:
        # ideal_content_for_file = """...PASTE THE IDEAL TXT CONTENT HERE..."""
        # with open(BENCHMARK_IDEAL_MERGED, "w", encoding="utf-8") as f:
        #     f.write(ideal_content_for_file)
        # print(f"A placeholder ideal merge file has been written. PLEASE REVIEW IT.")


    if not all(os.path.exists(p) for p in [BENCHMARK_OLD, BENCHMARK_MOD, BENCHMARK_NEW, BENCHMARK_IDEAL_MERGED]):
        print("ERROR: One or more benchmark files (Old, Mod, New, or Ideal Merged) not found.")
        print(f"Please ensure these files are in: {BENCHMARK_DIR}")
        missing_files = [p for p in [BENCHMARK_OLD, BENCHMARK_MOD, BENCHMARK_NEW, BENCHMARK_IDEAL_MERGED] if not os.path.exists(p)]
        print("Missing: \n - " + "\n - ".join(missing_files))
    else:
        print("All benchmark input files found. Running the new benchmark test...")
        run_new_benchmark_test(
            "Comprehensive PDS Merge Benchmark (File-Based Ideal)",
            BENCHMARK_OLD, BENCHMARK_MOD, BENCHMARK_NEW, BENCHMARK_IDEAL_MERGED
        )
        print("\n--- New Benchmark Run Complete ---")
        